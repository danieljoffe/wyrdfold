"""Guard: an API request field that no frontend code ever sends is inert.

Why this exists: in #780 `CoverLetterRequest.allow_stretch` was plumbed all the
way through the API — model, router, pipeline, prompt — and **nothing in the
frontend ever set it**. The feature could not fire in production. Every check
was green: jest mocks `fetch`, so no FE test notices a field it never sends,
and typecheck cannot see across the FE/API boundary at all. It was caught only
by a human reading the diff.

This is the field-level twin of
``apps/wyrdfold/src/app/(app)/jobs/__tests__/bffRoutesExist.spec.ts``, which
catches the route-level version of the same class of bug (the FE calling a
`/api/...` path with no Next route behind it — also shipped once, also green).

The check: every field on a Pydantic model used as a FastAPI request body must
appear somewhere in the frontend source. Test files are excluded from the
corpus on purpose — a field mentioned only in a spec still has no real sender.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_API = Path(__file__).resolve().parents[1] / "app"
_FE = Path(__file__).resolve().parents[2] / "wyrdfold/src"

# Fields the API accepts that the frontend deliberately never sends. Each entry
# is dead surface we have decided to keep — not a bug, but not free either. Add
# to this list only with a reason; a new unexplained entry means someone shipped
# half a feature.
_ALLOWED_WITHOUT_FE_SENDER: dict[tuple[str, str], str] = {
    ("TailorRequest", "critique"): "Server-side re-tailor loop passes it; no UI surfaces a critique box.",
    ("CoverLetterRequest", "critique"): "Same re-tailor loop; no UI surface.",
    ("TailorRequest", "page_budget"): "1-vs-2-page resume knob; UI has no control, always defaults to 2.",
    ("BatchRequest", "page_budget"): "Same knob on the batch path.",
}


def _model_fields() -> dict[str, list[str]]:
    """Every Pydantic BaseModel in app/models/ -> its declared field names."""
    out: dict[str, list[str]] = {}
    for path in _API.rglob("models/*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ClassDef) and any(
                isinstance(b, ast.Name) and b.id == "BaseModel" for b in node.bases
            ):
                out[node.name] = [
                    stmt.target.id
                    for stmt in node.body
                    if isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                ]
    return out


def _request_body_models(known: set[str]) -> dict[str, str]:
    """Models taken as a request body by a mutating route -> "file:path"."""
    found: dict[str, str] = {}
    for path in (_API / "routers").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            routes = [
                d
                for d in node.decorator_list
                if isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr in {"post", "put", "patch", "delete"}
            ]
            if not routes:
                continue
            route_path = ""
            if routes[0].args and isinstance(routes[0].args[0], ast.Constant):
                route_path = str(routes[0].args[0].value)
            for arg in list(node.args.args) + list(node.args.kwonlyargs):
                name = arg.annotation.id if isinstance(arg.annotation, ast.Name) else None
                if name in known and name not in found:
                    found[name] = f"{path.name}:{route_path or '/'}"
    return found


def _frontend_source() -> str:
    return "\n".join(
        p.read_text(errors="ignore")
        for p in _FE.rglob("*")
        if p.suffix in {".ts", ".tsx"} and "__tests__" not in str(p)
    )


def test_scan_preconditions_hold() -> None:
    """Assert the scan actually scanned. Without this the contract test below
    passes trivially in a partial checkout or after a refactor moves a
    directory — the failure mode called out in the repo's own notes about
    guards that cannot fail."""
    assert _FE.is_dir(), f"frontend source not found at {_FE}"
    corpus = _frontend_source()
    assert len(corpus) > 200_000, f"FE corpus implausibly small ({len(corpus)} chars)"
    fields = _model_fields()
    assert len(fields) > 50, f"only {len(fields)} Pydantic models parsed"
    bodies = _request_body_models(set(fields))
    assert len(bodies) > 20, f"only {len(bodies)} request-body models found"


def test_every_request_field_has_a_frontend_sender() -> None:
    fields = _model_fields()
    bodies = _request_body_models(set(fields))
    corpus = _frontend_source()

    inert: list[str] = []
    for model, where in sorted(bodies.items()):
        for field in fields[model]:
            if (model, field) in _ALLOWED_WITHOUT_FE_SENDER:
                continue
            if not re.search(rf"\b{re.escape(field)}\b", corpus):
                inert.append(f"{model}.{field} (accepted by {where}) — no frontend sender")

    assert not inert, (
        "These API request fields are inert — the backend accepts them but no "
        "frontend code sends them, so the feature cannot fire in production:\n  "
        + "\n  ".join(inert)
        + "\n\nEither wire the frontend, or add the field to "
        "_ALLOWED_WITHOUT_FE_SENDER with a reason."
    )


def test_allowlist_has_no_stale_entries() -> None:
    """An allowlisted field that IS now sent should leave the ledger, otherwise
    the list rots into a place where real bugs hide."""
    fields = _model_fields()
    corpus = _frontend_source()
    stale = [
        f"{model}.{field}"
        for (model, field) in _ALLOWED_WITHOUT_FE_SENDER
        if field in fields.get(model, []) and re.search(rf"\b{re.escape(field)}\b", corpus)
    ]
    assert not stale, f"allowlisted but now sent by the frontend — remove: {stale}"


def test_allowlist_entries_still_exist() -> None:
    """A renamed/removed field must not linger in the ledger."""
    fields = _model_fields()
    missing = [
        f"{model}.{field}"
        for (model, field) in _ALLOWED_WITHOUT_FE_SENDER
        if field not in fields.get(model, [])
    ]
    assert not missing, f"allowlist references fields that no longer exist: {missing}"
