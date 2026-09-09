"""Model factory.

Defaults to Amazon Bedrock (Claude Sonnet 4.6 via the global inference profile). If
``MODEL_PROVIDER=anthropic`` and ``ANTHROPIC_API_KEY`` are set, the Anthropic provider is
used instead; that import is optional and guarded.
"""

from __future__ import annotations

import os

from strands.models import BedrockModel
from strands.models.model import Model

DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-6"


def build_model() -> Model:
    """Return the configured Strands model. Always sets ``max_tokens`` explicitly."""
    provider = os.getenv("MODEL_PROVIDER", "bedrock").lower()
    if provider == "anthropic" and os.getenv("ANTHROPIC_API_KEY"):
        try:
            from strands.models.anthropic import AnthropicModel

            return AnthropicModel(
                client_args={"api_key": os.environ["ANTHROPIC_API_KEY"]},
                model_id=os.getenv("ANTHROPIC_MODEL_ID", "claude-sonnet-4-6"),
                max_tokens=4096,
                params={"temperature": 0.2},
            )
        except ImportError:  # pragma: no cover - optional extra not installed
            pass
    return BedrockModel(
        model_id=os.getenv("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
        region_name=os.getenv("AWS_REGION", "us-west-2"),
        max_tokens=4096,
        temperature=0.2,
    )
