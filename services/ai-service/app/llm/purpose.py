"""What the LLM is being used for - drives model choice and generation limits."""

from enum import StrEnum


class Purpose(StrEnum):
    SUMMARY = "summary"
    TRIAGE = "triage"
    INVESTIGATION = "investigation"
    EVALUATION = "evaluation"
    # EMBEDDING = "embedding"  # M5: separate embeddings factory, same provider registry

