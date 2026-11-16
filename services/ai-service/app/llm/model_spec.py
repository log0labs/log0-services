"""Provider + model id, parsed from ``provider:model`` strings (env or DB)."""

from pydantic import BaseModel


class ModelSpec(BaseModel):
    """Which vendor and which model id to call."""
    
    model_config = {"frozen": True}

    provider: str
    model: str
    
    @classmethod
    def parse(cls, text: str) -> "ModelSpec":
        provider, sep, model = text.partition(":")
        if not sep or not provider.strip() or not model.strip():
            raise ValueError(f"expected 'provider:model', got {text!r}")
        return cls(provider=provider.strip(), model=model.strip())
    
    def format(self) -> str:
        return f"{self.provider}:{self.model}"
