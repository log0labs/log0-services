import os

from app.config import Settings
from app.observability import configure_tracing


def test_configure_tracing_exports_langsmith_env(monkeypatch):
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)

    settings = Settings(
        groq_api_key="test-groq-key",
        langchain_tracing_v2=True,
        langchain_api_key="lsv2_test_key",
        langchain_project="test-project",
    )
    configure_tracing(settings)

    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGCHAIN_API_KEY"] == "lsv2_test_key"
    assert os.environ["LANGCHAIN_PROJECT"] == "test-project"


def test_configure_tracing_off(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    settings = Settings(groq_api_key="test-groq-key", langchain_tracing_v2=False)
    configure_tracing(settings)
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
