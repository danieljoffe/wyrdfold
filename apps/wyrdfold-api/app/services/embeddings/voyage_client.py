"""Real Voyage embeddings client.

Production implementation of the EmbeddingsClient Protocol. Uses the
official `voyageai` SDK's AsyncClient. Swap in via
`EMBEDDINGS_PROVIDER=voyage` env var; mock is the default until you
explicitly opt in.

Input type
----------
Voyage supports asymmetric embeddings via the `input_type` parameter:
`"document"` for stored text, `"query"` for search queries. Our chunk
write path embeds documents; a retrieval path (e.g. the pre-scan target
vector) embeds queries. The Protocol now exposes `input_type` (default
`"document"`), and this client threads it straight to the SDK — callers
pass the abstract `"document"`/`"query"` hint, not vendor-specific
vocabulary.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from voyageai.client_async import AsyncClient

from app.models.embeddings import EmbeddingModelId, EmbeddingResult, EmbeddingUsage
from app.services.embeddings.pricing import calculate_cost

# Voyage's documented per-call cap is 1000 inputs and 320k tokens.
# 128 keeps each sub-batch well below both limits and parallelizes
# better when callers send large batches (e.g. resume re-derive).
MAX_INPUTS_PER_CALL = 128


class VoyageEmbeddingsClient:
    """Implements the EmbeddingsClient Protocol. Production-ready."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        # voyageai's AsyncClient accepts api_key, timeout, max_retries.
        # It falls back to VOYAGE_API_KEY env var when api_key is None.
        self._client = AsyncClient(
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
        )

    async def _embed_one_batch(
        self,
        *,
        model: EmbeddingModelId,
        inputs: list[str],
        input_type: Literal["document", "query"],
    ) -> tuple[list[list[float]], int]:
        response: Any = await self._client.embed(
            texts=inputs,
            model=model,
            input_type=input_type,
        )
        return list(response.embeddings), int(response.total_tokens)

    async def embed(
        self,
        *,
        model: EmbeddingModelId,
        inputs: list[str],
        purpose: str,
        input_type: Literal["document", "query"] = "document",
    ) -> EmbeddingResult:
        if not inputs:
            # Don't call the API for an empty batch — save a roundtrip.
            return EmbeddingResult(
                embeddings=[],
                model=model,
                usage=EmbeddingUsage(input_tokens=0),
                cost_usd=0.0,
                latency_ms=0,
            )

        start = time.perf_counter()

        if len(inputs) <= MAX_INPUTS_PER_CALL:
            embeddings, total_tokens = await self._embed_one_batch(
                model=model, inputs=inputs, input_type=input_type
            )
        else:
            sub_batches = [
                inputs[i : i + MAX_INPUTS_PER_CALL]
                for i in range(0, len(inputs), MAX_INPUTS_PER_CALL)
            ]
            results = await asyncio.gather(
                *(
                    self._embed_one_batch(
                        model=model, inputs=sub, input_type=input_type
                    )
                    for sub in sub_batches
                )
            )
            embeddings = [vec for sub_embeds, _ in results for vec in sub_embeds]
            total_tokens = sum(tokens for _, tokens in results)

        latency_ms = int((time.perf_counter() - start) * 1000)
        usage = EmbeddingUsage(input_tokens=total_tokens)
        cost = calculate_cost(model, usage)

        return EmbeddingResult(
            embeddings=embeddings,
            model=model,
            usage=usage,
            cost_usd=cost,
            latency_ms=latency_ms,
        )
