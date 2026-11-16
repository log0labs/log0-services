"""Make a short-lived log0 access token for the MCP server (stdio), for local use.

    uv run python scripts/mcp_token.py <tenant_id> [VIEWER|ENGINEER|ADMIN] [hours]

The tools are read-only whatever the role, so VIEWER (the default) is enough. The token is a secret: it lets
whoever holds it read that tenant's incidents, logs and code through the tools. It expires after the given
number of hours (default 12, at most 168). Put it in the MCP client's configuration (see app/mcp/stdio.py).
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jwt

from app.config import get_settings


def main() -> None:
    args = sys.argv[1:]
    if not 1 <= len(args) <= 3:
        raise SystemExit(__doc__)
    tenant = UUID(args[0])
    role = (args[1] if len(args) > 1 else "VIEWER").upper()
    if role not in {"VIEWER", "ENGINEER", "ADMIN"}:
        raise SystemExit("role must be VIEWER, ENGINEER or ADMIN")
    hours = float(args[2]) if len(args) > 2 else 12.0
    if not 0 < hours <= 168:
        raise SystemExit("hours must be more than 0 and at most 168")
    now = datetime.now(tz=UTC)
    claims = {"sub": str(uuid4()), "tenantId": str(tenant), "role": role, "iat": now, "exp": now + timedelta(hours=hours)}
    print(jwt.encode(claims, get_settings().jwt_secret.get_secret_value(), algorithm="HS256"))


if __name__ == "__main__":
    main()
