"""Generate an AI summary and write it back to incident-service."""

import logging

from app.config import get_settings
from app.integrations.incident_service import patch_ai_summary
from app.llm.factory import chat_model_for_purpose
from app.llm.generation import generation_for_purpose
from app.llm.purpose import Purpose
from app.llm.resolver import PlatformLlmResolver
from app.schemas.summary import SummaryRequest
from app.summary.chain import build_summary_chain

logger = logging.getLogger(__name__)


def generate_and_store_summary(request: SummaryRequest) -> None:
    """Run LLM + callback. Errors are logged, not re-raised (matches Java ``AiSummaryService``)."""
    settings = get_settings()
    try:
        logger.info("Generating AI summary for incident %s", request.incident_id)
        model = chat_model_for_purpose(
            PlatformLlmResolver(settings),
            Purpose.SUMMARY,
            generation_for_purpose(settings, Purpose.SUMMARY),
        )
        summary_text = build_summary_chain(model).invoke(request)
        patch_ai_summary(settings, request.incident_id, summary_text)
    except Exception:
        logger.exception(
            "Failed to generate AI summary for incident %s",
            request.incident_id,
        )
