"""MockEmbeddingsClient — deterministic fake.

Returns vectors derived from a sha256 hash of each input, projected into
the target dimension as floats in [-1, 1]. Same input always yields the
same vector — useful for snapshot tests of retrieval. The vector depends
only on the text, NOT on ``input_type``: the mock stays deterministic so
a document and its matching query embed identically (cosine 1.0), which
keeps retrieval snapshots stable. The ``input_type`` is still recorded
in ``calls`` so a test can assert it was forwarded.

Token estimation: char-count / 4 (matches the LLM mock heuristic).
"""

from __future__ import annotations

import hashlib
from typing import Literal

from app.models.embeddings import EmbeddingModelId, EmbeddingResult, EmbeddingUsage
from app.services.embeddings.pricing import calculate_cost

DIMENSIONS: dict[EmbeddingModelId, int] = {
    "voyage-3": 1024,
    "voyage-3-lite": 512,
}


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _deterministic_vector(text: str, dim: int) -> list[float]:
    """Generate a stable pseudo-random vector for a given text + dim."""
    out: list[float] = []
    counter = 0
    while len(out) < dim:
        seed = f"{text}::{counter}".encode()
        digest = hashlib.sha256(seed).digest()
        for i in range(0, len(digest), 2):
            if len(out) >= dim:
                break
            sample = int.from_bytes(digest[i : i + 2], "big")
            out.append((sample / 65535.0) * 2 - 1)
        counter += 1
    return out


class MockEmbeddingsClient:
    def __init__(self, *, default_latency_ms: int = 30) -> None:
        self._default_latency_ms = default_latency_ms
        self.calls: list[dict[str, object]] = []

    async def embed(
        self,
        *,
        model: EmbeddingModelId,
        inputs: list[str],
        purpose: str,
        input_type: Literal["document", "query"] = "document",
    ) -> EmbeddingResult:
        dim = DIMENSIONS[model]
        embeddings = [_deterministic_vector(text, dim) for text in inputs]
        usage = EmbeddingUsage(input_tokens=sum(_approx_tokens(t) for t in inputs))

        self.calls.append(
            {
                "model": model,
                "purpose": purpose,
                "input_count": len(inputs),
                "input_type": input_type,
            }
        )

        return EmbeddingResult(
            embeddings=embeddings,
            model=model,
            usage=usage,
            cost_usd=calculate_cost(model, usage),
            latency_ms=self._default_latency_ms,
        )
