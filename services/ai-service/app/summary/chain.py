"""LCEL chain: incident context → structured summary → legacy plain text."""

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda

from app.schemas.summary import (
    IncidentSummary,
    SummaryRequest,
    format_incident_context,
    format_legacy_summary_text,
)

SYSTEM_PROMPT = """You are an incident analysis assistant for a software engineering team.
Given production incident data, produce a concise structured assessment.
Use conditional language for causes (may be, likely, possibly). Do not claim certainty
without evidence in the incident data."""

HUMAN_PROMPT = """{incident_context}"""

_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", SYSTEM_PROMPT),
        ("human", HUMAN_PROMPT),
    ]
)

def build_summary_chain(model: BaseChatModel):
    """Return a Runnable: SummaryRequest → legacy summary string."""
    structured_model = model.with_structured_output(IncidentSummary)
    
    def invoke(request: SummaryRequest) -> str:
        incident_context = format_incident_context(request)
        structured: IncidentSummary = (_prompt | structured_model).invoke(
            {"incident_context": incident_context}
        )
        return format_legacy_summary_text(structured)

    return RunnableLambda(invoke)
