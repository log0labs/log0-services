"""Run a project's tests in a throwaway, locked-down container. Optional, off by default.

Threat model: the code under test is somebody else's, and a model (steered by log text anyone may have written)
helped produce the change being tested. Assume it is hostile. What it must not be able to do: reach the network
while it runs, see this host's files or secrets, keep running after the time limit, use more than its share of
memory, CPU or processes, or become more privileged than an unprivileged user.

How, in layers (each one assumes the one before has failed):

1. **A different kernel view**: a container with every capability dropped, ``no-new-privileges``, a read-only root
   filesystem, a private tmpfs, an unprivileged user, private PID/IPC/mount namespaces and Docker's default seccomp
   profile. An operator can add a stronger runtime (gVisor ``runsc``, Kata) with ``SANDBOX_RUNTIME``; that is the
   recommended setting for anything beyond a developer machine.
2. **No network while the repository's code runs.** Dependencies are fetched in a separate first step that executes no
   repository code (see ``plan.py``), on an egress-restricted network chosen by the operator.
3. **Hard limits**: memory (no swap), CPU, process count, open files, wall-clock time, workdir size, captured output.
4. **Nothing carried in**: the container gets a disposable copy of the source and an explicit, fixed environment; no
   host environment variable, no Docker socket, no volume except that copy. The copy is deleted afterwards.
5. **A dedicated Docker daemon**: the ai-service never mounts ``/var/run/docker.sock`` (that is root on the host).
   It talks to a separate runner daemon through ``DOCKER_HOST`` (ssh or TLS), ideally on its own machine.
"""

import asyncio
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.config import Settings
from app.sandbox.plan import DEFAULT_IMAGES, SandboxPlan, plan_for
from app.sandbox.port import SandboxRun
from app.security.redaction import redact_text

logger = logging.getLogger(__name__)

NAME_PREFIX = "log0-sbx-"
UNPRIVILEGED_USER = "65534:65534"  # nobody
MAX_OUTPUT_TAIL = 6000
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class SandboxLimits:
    memory: str = "1g"
    cpus: str = "1.0"
    pids: int = 256
    open_files: int = 1024
    tmpfs_mb: int = 512
    test_timeout_seconds: float = 300.0
    prepare_timeout_seconds: float = 300.0
    max_workdir_mb: int = 700


@dataclass(frozen=True)
class SandboxConfig:
    docker_binary: str = "docker"
    docker_host: str = ""  # a dedicated runner daemon, never the local socket of the ai-service host
    runtime: str = ""  # "" is Docker's default (runc); "runsc" is gVisor
    prepare_network: str = ""  # a pre-created, egress-restricted Docker network for the dependency step
    prepare_proxy: str = ""  # an allowlisting proxy reachable on that network
    prepare_open: bool = False  # explicitly accept an unrestricted network for the dependency step (development only)
    images: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_IMAGES))
    limits: SandboxLimits = field(default_factory=SandboxLimits)

    @classmethod
    def from_settings(cls, settings: Settings) -> "SandboxConfig":
        return cls(
            docker_host=settings.sandbox_docker_host,
            runtime=settings.sandbox_runtime,
            prepare_network=settings.sandbox_prepare_network,
            prepare_proxy=settings.sandbox_prepare_proxy,
            prepare_open=settings.sandbox_prepare_open,
            images={**DEFAULT_IMAGES, **settings.sandbox_images},
            limits=SandboxLimits(
                memory=settings.sandbox_memory,
                cpus=settings.sandbox_cpus,
                pids=settings.sandbox_pids,
                test_timeout_seconds=settings.sandbox_test_timeout_seconds,
                prepare_timeout_seconds=settings.sandbox_prepare_timeout_seconds,
                max_workdir_mb=settings.sandbox_max_workdir_mb,
            ),
        )


def run_args(
    *,
    name: str,
    image: str,
    command: tuple[str, ...],
    env: dict[str, str],
    host_dir: str,
    subdir: str,
    network: str,
    limits: SandboxLimits,
    runtime: str = "",
) -> list[str]:
    """The ``docker run`` arguments for one locked-down step. Pure, so every flag is covered by a test."""
    args = [
        "run",
        "--name", name,
        "--init",
        "--user", UNPRIVILEGED_USER,
        "--network", network,
        "--read-only",
        "--tmpfs", f"/tmp:rw,exec,nosuid,nodev,size={limits.tmpfs_mb}m",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(limits.pids),
        "--memory", limits.memory,
        "--memory-swap", limits.memory,
        "--cpus", limits.cpus,
        "--ulimit", f"nofile={limits.open_files}:{limits.open_files}",
        "--ulimit", "core=0",
        "--hostname", "sandbox",
        "--volume", f"{host_dir}:/work:rw",
        "--workdir", str(PurePosixPath("/work") / subdir),
        "--env", "HOME=/tmp",
        "--env", "LANG=C.UTF-8",
    ]
    if runtime:
        args += ["--runtime", runtime]
    for key, value in sorted(env.items()):
        args += ["--env", f"{key}={value}"]
    return [*args, image, *command]


def safe_subdir(subdir: str) -> str:
    """A relative path inside the project, or ``""``. Anything that could leave it is refused."""
    normal = subdir.replace("\\", "/")
    if normal.startswith("/") or re.match(r"^[A-Za-z]:", normal):
        raise ValueError("the project folder must be a relative path inside the repository")
    path = PurePosixPath(normal.strip("/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("the project folder must be a relative path inside the repository")
    return "" if str(path) == "." else str(path)


def _tidy(text: str) -> str:
    text = _ANSI.sub("", text).replace("\r", "")
    return redact_text(text[-MAX_OUTPUT_TAIL:])


def _prepare_cache(work: Path) -> None:
    """The folders the toolchains are told to use for caches and temporary files must exist (the root filesystem is read-only)."""
    for name in ("tmp", "gomod", "gobuild", "npm", "cargo", "target", "site"):
        (work / ".cache" / name).mkdir(parents=True, exist_ok=True)


def _make_writable(root: Path) -> None:
    """The container runs as an unprivileged user and the copy belongs to someone else: open it up. It is disposable."""
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        try:
            path.chmod(path.stat().st_mode | stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
        except OSError:
            continue


def _directory_mb(root: Path) -> float:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total / 1_000_000


@dataclass(frozen=True)
class _Finished:
    exit_code: int | None
    output: str
    timed_out: bool
    oom_killed: bool


class DockerSandbox:
    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._config = config or SandboxConfig()

    @property
    def available(self) -> bool:
        return shutil.which(self._config.docker_binary) is not None

    async def run(self, *, source: Path, subdir: str) -> SandboxRun | None:
        started = time.monotonic()
        folder = safe_subdir(subdir)
        plan = plan_for(source / folder, self._config.images)
        if plan is None:
            return None
        parent = Path(tempfile.mkdtemp(prefix="log0-sandbox-"))
        work = parent / "work"
        try:
            await asyncio.to_thread(shutil.copytree, source, work, symlinks=True, ignore=shutil.ignore_patterns(".git"))
            await asyncio.to_thread(_prepare_cache, work)
            await asyncio.to_thread(_make_writable, work)
            problem = await self._prepare(plan, work, folder)
            if problem is not None:
                return SandboxRun(plan.name, "error", None, problem, time.monotonic() - started)
            result = await self._exec(plan.image, plan.env, plan.test, work, folder, network="none", timeout=self._config.limits.test_timeout_seconds)
        except Exception:
            logger.exception("the sandbox could not run the tests")
            return SandboxRun(plan.name, "error", None, "the sandbox could not run the tests", time.monotonic() - started)
        finally:
            await asyncio.to_thread(shutil.rmtree, parent, ignore_errors=True)
        seconds = time.monotonic() - started
        if result.timed_out:
            return SandboxRun(plan.name, "timeout", None, _tidy(result.output), seconds)
        note = "\n(the tests ran out of memory)" if result.oom_killed else ""
        status = "passed" if result.exit_code == 0 else "failed"
        return SandboxRun(plan.name, status, result.exit_code, _tidy(result.output + note), seconds)

    async def _prepare(self, plan: SandboxPlan, work: Path, folder: str) -> str | None:
        """Fetch dependencies without running repository code. ``None`` on success or when there is nothing to fetch."""
        cfg = self._config
        if plan.prepare is None or not (cfg.prepare_network or cfg.prepare_open):
            return None  # without an allowed network the tests run on what the repository already holds (vendored, stdlib)
        network = cfg.prepare_network or "bridge"
        env = dict(plan.env)
        if cfg.prepare_proxy:
            env |= {"HTTP_PROXY": cfg.prepare_proxy, "HTTPS_PROXY": cfg.prepare_proxy, "http_proxy": cfg.prepare_proxy, "https_proxy": cfg.prepare_proxy}
        result = await self._exec(plan.image, env, plan.prepare, work, folder, network=network, timeout=cfg.limits.prepare_timeout_seconds)
        if result.timed_out or result.exit_code != 0:
            return "fetching the dependencies failed: " + _tidy(result.output)
        if await asyncio.to_thread(_directory_mb, work) > cfg.limits.max_workdir_mb:
            return f"the project and its dependencies are larger than {cfg.limits.max_workdir_mb} MB"
        return None

    async def _exec(
        self, image: str, env: dict[str, str], command: tuple[str, ...], work: Path, folder: str, *, network: str, timeout: float
    ) -> _Finished:
        cfg = self._config
        name = f"{NAME_PREFIX}{uuid.uuid4().hex[:12]}"
        args = run_args(
            name=name,
            image=image,
            command=command,
            env=env,
            host_dir=str(work),
            subdir=folder,
            network=network,
            limits=cfg.limits,
            runtime=cfg.runtime,
        )
        return await asyncio.to_thread(self._exec_blocking, name, args, timeout)

    def _docker_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self._config.docker_host:
            env["DOCKER_HOST"] = self._config.docker_host
        return env

    def _exec_blocking(self, name: str, args: list[str], timeout: float) -> _Finished:
        """Run one container to the end or the time limit, keeping only the end of its output. Always removes the container.

        Threads, not asyncio subprocesses: they work on every event loop (the Windows selector loop cannot spawn processes).
        """
        cfg = self._config
        process = subprocess.Popen(
            [cfg.docker_binary, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=self._docker_env()
        )
        tail: deque[str] = deque()
        kept = 0

        def read() -> None:
            nonlocal kept
            assert process.stdout is not None
            while chunk := process.stdout.read(4096):
                text = chunk.decode("utf-8", errors="replace")
                tail.append(text)
                kept += len(text)
                while kept > MAX_OUTPUT_TAIL * 2 and len(tail) > 1:  # keep only the end: a noisy test cannot fill memory
                    kept -= len(tail.popleft())

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._docker_blocking("kill", name)
            process.kill()
            process.wait()
        reader.join(10)
        oom = self._docker_blocking("inspect", "--format", "{{.State.OOMKilled}}", name).strip() == "true"
        self._docker_blocking("rm", "-f", name)
        return _Finished(None if timed_out else process.returncode, "".join(tail), timed_out, oom)

    def _docker_blocking(self, *args: str) -> str:
        """A small docker command (kill, inspect, rm); failures are not interesting here."""
        try:
            done = subprocess.run(
                [self._config.docker_binary, *args], capture_output=True, text=True, timeout=30, env=self._docker_env(), check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return done.stdout


def build_sandbox(settings: Settings) -> DockerSandbox | None:
    """The sandbox when the server switched it on and a docker client is installed; otherwise none (and a clear log line)."""
    if not settings.sandbox_enabled:
        return None
    sandbox = DockerSandbox(SandboxConfig.from_settings(settings))
    if not sandbox.available:
        logger.warning("SANDBOX_ENABLED is set but no docker client was found: tests will not run in a sandbox")
        return None
    if not settings.sandbox_docker_host:
        logger.warning("the sandbox uses the local Docker daemon: use a dedicated runner daemon (SANDBOX_DOCKER_HOST) outside development")
    return sandbox
