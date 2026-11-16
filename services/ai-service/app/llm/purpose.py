"""What the LLM is being used for - drives model choice and generation limits."""

from enum import StrEnum
from typing import Literal

# How a model is asked for structured output. Not every model supports every method (see config.py).
StructuredOutputMethod = Literal["json_schema", "function_calling"]


class Purpose(StrEnum):
    SUMMARY = "summary"
    TRIAGE = "triage"
    INVESTIGATION = "investigation"
    EVALUATION = "evaluation"
    # EMBEDDING = "embedding"  # M5: separate embeddings factory, same provider registry

