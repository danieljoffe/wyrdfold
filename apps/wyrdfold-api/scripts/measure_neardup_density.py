"""One-off measurement (#60, Phase 1): near-dup density over ``job_embeddings``.

READ-ONLY — does NOT write anything. Decides whether a *semantic grade-reuse*
cache (Phase 1.5) is worth building: for each live job, find its nearest
neighbor by cosine and count near-identical pairs (cosine >= 0.95), split by
whether the pair shares ``(company_name, title)``.

  - SAME (company, title)  → already collapsed by the poller's
    ``_dedupe_by_content`` (a re-listing of one role). NOT incremental.
  - CROSS (company, title) → the INCREMENTAL dup class: reworded reposts,
    staffing-agency duplicates, the same role across companies. This is the
    upper-bound LLM-grade saving a semantic-reuse cache would add BEYOND the
    existing exact dedup. Big → build Phase 1.5; small → skip.

Needs the table populated first — run ``backfill_job_embeddings.py`` (or let the
poller populate with PRESCAN_EMBED_ENABLED on). With an empty table this prints
zeros and exits.

Run with prod env (real Supabase). Must run from a checkout that HAS the
pre-scan code (develop or main):

    git checkout develop && git pull
    cd apps/wyrdfold-api && railway run uv run python scripts/measure_neardup_density.py

Tunables (env or flags):
    --threshold F    cosine cutoff for "near-identical" (default 0.95).
    --model NAME     which embedding model's vectors to read (default voyage-3).
    --limit N        cap jobs scanned (0 = all; useful for a quick read).

Scale note
----------
This computes the all-pairs nearest neighbor in pure Python (stdlib only — the
API has no numpy / psycopg dependency). That is O(n^2) and fine for the current
beta-scale corpus. At larger scale, run the equivalent pgvector self-join in the
Supabase SQL editor instead — it uses the HNSW index:

    WITH nn AS (
      SELECT je.job_posting_id AS id,
             (SELECT je2.job_posting_id
                FROM job_embeddings je2
                JOIN jobs j2 ON j2.id = je2.job_posting_id
               WHERE je2.model = je.model
                 AND je2.job_posting_id <> je.job_posting_id
                 AND j2.archived_at IS NULL
               ORDER BY je2.embedding <=> je.embedding
               LIMIT 1) AS nn_id,
             je.embedding AS emb
        FROM job_embeddings je
        JOIN jobs j ON j.id = je.job_posting_id
       WHERE je.model = 'voyage-3' AND j.archived_at IS NULL
    )
    SELECT
      sum(((1 - (nn.emb <=> n2.embedding)) >= 0.95)::int)                       AS near_pairs,
      sum((((1 - (nn.emb <=> n2.embedding)) >= 0.95)
           AND (j1.company_name IS NOT DISTINCT FROM j2.company_name)
           AND (j1.title        IS NOT DISTINCT FROM j2.title))::int)           AS same_co_title,
      sum((((1 - (nn.emb <=> n2.embedding)) >= 0.95)
           AND NOT (j1.company_name IS NOT DISTINCT FROM j2.company_name
                    AND j1.title    IS NOT DISTINCT FROM j2.title))::int)       AS cross_co_title
    FROM nn
    JOIN job_embeddings n2 ON n2.job_posting_id = nn.nn_id AND n2.model = 'voyage-3'
    JOIN jobs j1 ON j1.id = nn.id
    JOIN jobs j2 ON j2.id = nn.nn_id;
"""

from __future__ import annotations

import argparse
import ast
import math
import os
from typing import Any

from app.services.embeddings.job_embeddings import DEFAULT_MODEL
from app.supabase_pool import get_supabase_pool, init_supabase

_PAGE = 1000


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Measure near-dup density (#60).")
    p.add_argument(
        "--threshold",
        type=float,
        default=float(os.environ.get("NEARDUP_THRESHOLD", "0.95")),
        help="Cosine cutoff for a near-identical pair.",
    )
    p.add_argument("--model", default=DEFAULT_MODEL, help="Embedding model to read.")
    p.add_argument("--limit", type=int, default=0, help="Cap jobs scanned (0 = all).")
    return p.parse_args()


def _parse_vector(raw: Any) -> list[float] | None:
    """pgvector comes back over PostgREST as a '[0.1,0.2,...]' string (or a
    list if the client already parsed it). Return a float list, or None if
    unparseable."""
    if raw is None:
        return None
    if isinstance(raw, list):
        return [float(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return None
        if isinstance(parsed, (list, tuple)):
            return [float(x) for x in parsed]
    return None


def _fetch(sb: Any, *, model: str, limit: int) -> list[dict[str, Any]]:
    """Join job_embeddings → jobs for LIVE jobs of one model.

    PostgREST embedded-resource select pulls the parent job's company_name +
    title alongside each vector in one round trip.
    """
    out: list[dict[str, Any]] = []
    start = 0
    while True:
        end = start + _PAGE - 1
        resp = (
            sb.table("job_embeddings")
            .select("job_posting_id, embedding, jobs!inner(company_name, title, archived_at)")
            .eq("model", model)
            .is_("jobs.archived_at", "null")
            .range(start, end)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            break
        out.extend(rows)
        if limit and len(out) >= limit:
            return out[:limit]
        if len(rows) < _PAGE:
            break
        start += _PAGE
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def main() -> None:
    args = _parse_args()

    init_supabase()
    sb = get_supabase_pool()
    if sb is None:
        raise SystemExit("Supabase not configured (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)")

    print(f"Loading live job vectors (model={args.model}, threshold={args.threshold})...")
    raw_rows = _fetch(sb, model=args.model, limit=args.limit)

    vecs: list[list[float]] = []
    keys: list[tuple[str | None, str | None]] = []
    for r in raw_rows:
        vec = _parse_vector(r.get("embedding"))
        if vec is None:
            continue
        job = r.get("jobs") or {}
        vecs.append(vec)
        keys.append((job.get("company_name"), job.get("title")))

    n = len(vecs)
    print(f"Loaded {n} usable vectors.\n")
    if n < 2:
        print("Not enough vectors to measure near-dup density.")
        return

    near_pairs = 0
    same_co_title = 0
    cross_co_title = 0

    # For each job, its single nearest neighbor (excluding itself). O(n^2);
    # see the module docstring for the indexed SQL alternative at scale.
    for i in range(n):
        best_j = -1
        best_sim = -2.0
        for j in range(n):
            if i == j:
                continue
            sim = _cosine(vecs[i], vecs[j])
            if sim > best_sim:
                best_sim = sim
                best_j = j
        if best_j < 0 or best_sim < args.threshold:
            continue
        near_pairs += 1
        same = keys[i] == keys[best_j]
        if same:
            same_co_title += 1
        else:
            cross_co_title += 1

    pct = (100.0 * near_pairs / n) if n else 0.0
    print(f"Jobs scanned:                         {n}")
    print(f"Jobs whose NN cosine >= {args.threshold}:        {near_pairs} ({pct:.1f}%)")
    print(f"  - SAME (company,title)  [exact-dedup]:   {same_co_title}")
    print(f"  - CROSS (company,title) [INCREMENTAL]:   {cross_co_title}")
    print(
        "\nThe CROSS count is the upper-bound LLM-grade saving a semantic "
        "grade-reuse cache (Phase 1.5) would add beyond the existing exact "
        "dedup. Big → build it; small → skip."
    )


if __name__ == "__main__":
    main()
