"""Pydantic models for the embeddings layer (#185 P2b).

Embeddings are conceptually different from LLM completions but share
the same cost-tracking surface (llm_costs). The model column there
holds either a Claude ID or a Voyage ID — disambiguated at the call
site, opaque at the read layer.
"""

from typing import Literal

from pydantic import BaseModel

EmbeddingModelId = Literal["voyage-3", "voyage-3-lite", "voyage-3.5", "voyage-3.5-lite"]
"""Voyage models we target. Extend as new ones land.

``voyage-3``/``voyage-3-lite`` were RETIRED by Voyage (rolling rejection
observed 2026-07-09: ``InvalidRequestError: Model voyage-3 is not
supported``). They stay in the Literal because existing
``job_embeddings.model`` rows reference them — never embed with them again.
``voyage-3.5`` is the drop-in successor (1024-dim default, same price), but
its vector SPACE is not comparable to voyage-3's — never cosine across
models.
"""


class EmbeddingUsage(BaseModel):
    input_tokens: int = 0


class EmbeddingResult(BaseModel):
    embeddings: list[list[float]]
    """One vector per input string, in the same order."""

    model: EmbeddingModelId
    usage: EmbeddingUsage
    cost_usd: float
    latency_ms: int
