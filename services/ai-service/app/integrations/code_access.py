"""The production ``CodeAccess``: service -> mapped repository -> decrypted token -> local checkout.

It chains the pieces built in 3.1 to 3.6:

1. the service's repository mapping and the repository connection (``ai.service_repository``,
   ``ai.repository_connection``), both read **for this tenant only**,
2. the repository token, decrypted with the secret box under the context it was stored with,
3. ``RepoCache.ensure_checkout``: clone or update, locked, returning the commit sha,
4. the code and git tools over that checkout, scoped to the service's folder.

Nothing here logs or returns the token. Every failure is a ``CodeAccessError`` with a message that
is safe to show.
"""

import logging
from uuid import UUID

from app.graph.code_access import CodeAccessError, CodeCheckout
from app.integrations.repo_cache import CheckoutError, RepoCache, token_for_connection
from app.runner import SessionFactory
from app.schemas.domain import RepoContext
from app.security.crypto import CryptoError, SecretBox
from app.store import repositories as repository_store
from app.tools.code import CodeTools
from app.tools.git import GitTools

logger = logging.getLogger(__name__)


class DbCodeAccess:
    def __init__(
        self,
        sessions: SessionFactory,
        box: SecretBox | None,
        cache: RepoCache,
        *,
        git_binary: str = "git",
        rg_binary: str = "rg",
    ) -> None:
        self._sessions = sessions
        self._box = box
        self._cache = cache
        self._git = git_binary
        self._rg = rg_binary

    async def open(self, *, tenant_id: UUID, service_name: str) -> CodeCheckout | None:
        async with self._sessions() as session:
            mapping = await repository_store.get_service(session, tenant_id=tenant_id, service_name=service_name)
            if mapping is None:
                return None
            connection = await repository_store.get(session, tenant_id=tenant_id, repository_id=mapping.repository_id)
        if connection is None:  # disconnected between the two reads
            return None

        if self._box is None:
            raise CodeAccessError("secret storage is not configured on this server, so the repository token cannot be used")
        try:
            token = token_for_connection(connection, self._box)
        except CryptoError:
            raise CodeAccessError("the stored repository token could not be read: reconnect the repository") from None
        try:
            checkout = await self._cache.ensure_checkout(connection, token)
        except CheckoutError as exc:  # its message is already scrubbed of the token
            raise CodeAccessError(f"the repository could not be fetched ({exc})") from None

        logger.info("code checkout ready: tenant=%s repository=%s sha=%s", tenant_id, connection.id, checkout.sha[:8])
        prefix = mapping.path_prefix
        return CodeCheckout(
            repo=RepoContext(
                repo=f"{connection.owner}/{connection.name}",
                branch=checkout.branch,
                sha=checkout.sha,
                path_prefix=prefix,
            ),
            code=CodeTools(checkout.path, prefix=prefix, git_binary=self._git, rg_binary=self._rg),
            git=GitTools(checkout.path, prefix=prefix, git_binary=self._git),
        )
