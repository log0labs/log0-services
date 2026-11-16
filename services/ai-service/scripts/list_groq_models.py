"""Print Groq chat model ids available to your API key (uses GROQ_API_KEY from .env)."""

import sys
from pathlib import Path

# Allow ``uv run python scripts/list_groq_models.py`` from the service root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from groq import Groq

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    client = Groq(api_key=settings.groq_api_key.get_secret_value())
    for model in sorted(m.id for m in client.models.list().data):
        print(model)


if __name__ == "__main__":
    main()
