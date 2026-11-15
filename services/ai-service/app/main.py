"""FastAPI entry point: ``uvicorn app.main:app``."""

import logging
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from starlette.routing import Route

from app.api import (
    ai_settings,
    deploy_targets,
    github_app,
    github_webhook,
    health,
    investigations,
    pull_requests,
    remediations,
    repositories,
    runbooks,
    summaries,
)
from app.config import get_settings
from app.db.session import create_db_engine, create_session_factory
from app.deploy.executor import RemediationExecutor
from app.deploy.graph import build_remediation_graph
from app.deploy.runner import RemediationRunner
from app.graph.builder import build_graph
from app.graph.checkpoint import open_checkpointer
from app.graph.executor import GraphExecutor
from app.integrations.clickhouse import LogStore
from app.integrations.code_access import DbCodeAccess
from app.integrations.github import GitHubClient
from app.integrations.github_actions import GitHubActionsProvider
from app.integrations.github_app import GitHubAppAuth
from app.integrations.github_app_client import GitHubAppClient
from app.integrations.github_deploys import GitHubDeploys
from app.integrations.github_writer import GitHubWriter
from app.integrations.incident_db import IncidentReader
from app.integrations.incident_service import IncidentApiClient
from app.integrations.repo_cache import RepoCache
from app.llm.connection_check import check_connection
from app.llm.models import PurposeChatModels
from app.mcp.http import LazyMount, build_http_app
from app.mcp.server import McpBackends, build_mcp_server
from app.observability import configure_tracing
from app.publishing.github_publisher import GitHubPublisher
from app.retrieval.embeddings import build_embedder
from app.retrieval.memory import PgMemory
from app.retrieval.runbooks import RunbookSearcher
from app.runner import InvestigationRunner
from app.sandbox.docker import build_sandbox
from app.security.crypto import secret_box_from_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = get_settings()
    configure_tracing(settings)
    engine = create_db_engine(settings)
    session_factory = create_session_factory(engine)
    app.state.db_engine = engine
    app.state.async_session_factory = session_factory
    log_store = LogStore(settings)
    app.state.log_store = log_store
    incident_reader = IncidentReader(settings)
    app.state.incident_reader = incident_reader
    incident_api = IncidentApiClient(settings)
    app.state.incident_api = incident_api
    # Tenant settings and repositories (M3). No master key configured: the service still starts and
    # the endpoints that must store or read a tenant secret answer 503. A key that is set but
    # invalid is a configuration error and stops the start-up instead.
    app.state.secret_box = secret_box_from_settings(settings) if settings.secrets_keys else None
    app.state.github = GitHubClient(settings)
    github_app_auth = GitHubAppAuth(settings)
    app.state.github_app = GitHubAppClient(settings, github_app_auth)
    sandbox = build_sandbox(settings)
    github_writer = GitHubWriter(settings, github_app_auth)
    publisher = GitHubPublisher(
        session_factory,
        github_writer,
        enabled=settings.pr_writes_enabled and github_app_auth.configured,
        disabled_tenants=frozenset(settings.pr_writes_disabled_tenants),
    )
    app.state.connection_checker = check_connection
    app.state.embedder = build_embedder(settings)  # runbook search and memory (M5)
    app.state.memory = PgMemory(session_factory, app.state.embedder)

    runbook_search = RunbookSearcher(session_factory, app.state.embedder)
    code_access = DbCodeAccess(
        session_factory,
        app.state.secret_box,
        RepoCache(settings),
        git_binary=settings.git_binary,
        rg_binary=settings.rg_binary,
    )

    async with AsyncExitStack() as stack:
        if settings.mcp_http_enabled:
            mcp_server = build_mcp_server(
                McpBackends(
                    logs=log_store,
                    incidents=incident_api,
                    history=incident_reader,
                    session_factory=session_factory,
                    runbooks=runbook_search,
                    code=code_access,
                )
            )
            app.state.mcp_mount.target = build_http_app(mcp_server, secret=settings.jwt_secret.get_secret_value())
            await stack.enter_async_context(mcp_server.session_manager.run())
        executor: GraphExecutor | None = None
        if settings.investigation_graph_enabled:
            # Opens the Postgres checkpointer (creating its tables on first start) and keeps it
            # open until the stack unwinds, which is after the runner has stopped.
            saver = await stack.enter_async_context(open_checkpointer(settings))
            executor = GraphExecutor(
                build_graph(saver),
                logs=log_store,
                incidents=incident_api,
                history=incident_reader,
                models=PurposeChatModels(settings),
                prices=settings.llm_prices,
                require_review=settings.investigation_review_gate,
                runbooks=runbook_search,
                memory=app.state.memory,
                settings=settings,
                code=code_access,
                secret_box=app.state.secret_box,
                publisher=publisher,
                sandbox=sandbox,
            )
        app.state.graph_executor = executor
        remediation_runner = _build_remediation_runner(app, settings, session_factory, github_writer, stack_saver=saver if executor else None, logs=log_store, incidents=incident_api)
        app.state.remediation_runner = remediation_runner
        runner = InvestigationRunner(
            session_factory,
            max_concurrent=settings.investigation_max_concurrent,
            execute=executor.run if executor else None,  # None: the runner's no-op stub
        )
        app.state.investigation_runner = runner
        await runner.recover_on_startup()
        if executor:
            await remediation_runner.recover_on_startup()
            remediation_runner.start_sweeper()
        yield
        await remediation_runner.shutdown()
        await runner.shutdown()

    if hasattr(app.state.embedder, "close"):
        await app.state.embedder.close()
    await log_store.close()
    await incident_reader.close()
    await engine.dispose()


def _build_remediation_runner(app: FastAPI, settings, session_factory, writer: GitHubWriter, *, stack_saver, logs, incidents) -> RemediationRunner:
    """The runner of deploys (V3). Without a checkpointer (the graph switched off, as in most tests) it runs nothing."""

    async def nothing(*_args) -> None:
        return None

    execute = nothing
    if stack_saver is not None:
        execute = RemediationExecutor(
            build_remediation_graph(stack_saver),
            provider=GitHubActionsProvider(writer),
            deploys=GitHubDeploys(writer),
            checks=writer,
            installations=app.state.github_app,
            logs=logs,
            incidents=incidents,
            settings=settings,
        ).run
    return RemediationRunner(session_factory, execute, sweep_seconds=settings.deploy_sweep_seconds)


def create_app() -> FastAPI:
    """Build and wire the FastAPI application."""
    app = FastAPI(
        title="log0 AI Service",
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.include_router(health.router)
    app.include_router(summaries.router)
    app.include_router(investigations.router)
    app.include_router(pull_requests.router)
    app.include_router(repositories.router)
    app.include_router(repositories.services_router)
    app.include_router(github_app.router)
    app.include_router(deploy_targets.router)
    app.include_router(remediations.router)
    app.include_router(github_webhook.router)
    app.include_router(ai_settings.router)
    app.include_router(runbooks.router)
    app.state.mcp_mount = LazyMount()  # the endpoint is plugged in at start-up when MCP_HTTP_ENABLED is on
    # an exact route, not a mount: a mount would redirect POST /mcp to /mcp/, which MCP clients do not follow
    app.router.routes.append(Route("/mcp", endpoint=app.state.mcp_mount))
    return app


app = create_app()
