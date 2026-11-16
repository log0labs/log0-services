"""Print a new master key for SECRETS_KEYS.

    uv run python scripts/new_secret_key.py

The key is printed once and stored nowhere: copy it into your .env (never commit it) as, for example,
    SECRETS_KEYS={"k1": "<the printed key>"}
    SECRETS_ACTIVE_KEY_ID=k1
Losing the key makes every secret encrypted with it unreadable; keep a copy in your password manager.
To rotate: generate a second key, add it as "k2", set SECRETS_ACTIVE_KEY_ID=k2 and keep "k1" in
SECRETS_KEYS until every stored secret has been re-encrypted.
"""

from cryptography.fernet import Fernet

if __name__ == "__main__":
    print(Fernet.generate_key().decode())
