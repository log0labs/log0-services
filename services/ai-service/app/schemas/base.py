"""Shared Pydantic base for every JSON contract the service exposes or consumes.

log0's Java services and the console speak camelCase JSON; Python code uses
snake_case. This base maps between the two so each DTO only declares fields.
"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Accepts and emits camelCase JSON while exposing snake_case attributes."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )
