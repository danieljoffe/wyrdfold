"""The privacy boundary around the embedding provider (#439 legal review).

The Privacy Policy tells users that the embedding provider receives "extracts
of your experience profile" — role titles, employers, skills, outcomes — and
not their identity. That is true today for a structural reason rather than a
deliberate one: ``OptimizedPayload`` has no contact fields to leak, so the
chunk builder cannot put a name or an email into an embedding request even by
accident.

Structural truths stop being true when someone adds a field. These pin the
boundary so that a change which would start sending contact details to a third
party fails here instead of silently making the policy false.

Reviewed externally with the specific question "whether those extracts contain
names/contact information" — this file is the answer, kept executable.
"""

from __future__ import annotations

import inspect

from app.models.experience import OptimizedPayload, Outcome, Role, Skill
from app.services.experience.chunks import chunks_for_optimized

# Field names that carry identity rather than experience. Contact details live
# on ``user_profiles`` (name, email, phone_number, location, linkedin_url,
# website_url) and must stay there — that table is never embedded.
_CONTACT_FIELDS = frozenset(
    {
        "name",
        "full_name",
        "email",
        "email_address",
        "phone",
        "phone_number",
        "address",
        "location",
        "linkedin_url",
        "website_url",
        "url",
    }
)

# ``Skill.name`` is a skill's name ("Python"), not a person's. Excluded by
# model rather than by field name, so adding ``name`` anywhere else still trips.
_ALLOWED_NAME_FIELD_MODELS = frozenset({"Skill", "Annotation"})


def _identity_fields(model: type) -> set[str]:
    return {
        f
        for f in model.model_fields
        if f in _CONTACT_FIELDS
        and not (f == "name" and model.__name__ in _ALLOWED_NAME_FIELD_MODELS)
    }


def test_the_embedded_payload_has_no_contact_fields() -> None:
    """The boundary at the schema level.

    If this fails, someone added identity to the structure we embed. Either
    strip it before ``chunks_for_optimized``, or update the Privacy Policy —
    but do not let the policy and the wire disagree.
    """
    for model in (OptimizedPayload, Role, Skill, Outcome):
        assert not _identity_fields(model), (
            f"{model.__name__} gained a contact field "
            f"{sorted(_identity_fields(model))}; it would reach the embedding "
            "provider via chunks_for_optimized"
        )


def test_chunk_text_carries_experience_not_identity() -> None:
    """The boundary at the value level, with identity-shaped data planted.

    Every string below is something a resume plausibly contains. None of it is
    reachable through the optimized payload, so none of it may appear in a
    chunk. Asserting on a payload built from the real models means a new field
    would have to be added here too — which is the point.
    """
    payload = OptimizedPayload(
        summary="Backend engineer, ten years, payments and identity systems.",
        roles=[
            Role(
                id="r1",
                company="Initech",
                title="Staff Engineer",
                start="2019",
                end="2024",
                summary="Led the payments rewrite.",
                skills=["Python", "Postgres"],
            )
        ],
        skills=[Skill(name="Python", years=10.0)],
        outcomes=[Outcome(description="Cut checkout latency", metric="p99", value="-40%")],
    )

    chunks = chunks_for_optimized(payload)
    assert chunks, "no chunks produced — the assertions below would be vacuous"

    blob = "\n".join(
        [c.content for c in chunks] + [str(c.metadata or {}) for c in chunks]
    ).lower()

    # The experience DID make it through — otherwise this test proves nothing
    # about what was filtered, only that the builder returned little.
    assert "staff engineer" in blob
    assert "initech" in blob
    assert "python" in blob

    # Identity did not, because it was never in the payload to begin with.
    for planted in (
        "jane doe",
        "jane@example.com",
        "+1 555",
        "linkedin.com/in/",
        "1 example st",
    ):
        assert planted not in blob, f"identity-shaped value {planted!r} reached a chunk"


def test_the_embedding_request_carries_no_user_identifier() -> None:
    """We send text and a model. Not who the text is about.

    A per-user id on the request would let the provider correlate embeddings
    into a profile, which is a different privacy posture than the one the
    policy describes.
    """
    from app.services.embeddings.client import EmbeddingsClient

    params = set(inspect.signature(EmbeddingsClient.embed).parameters)
    for identifier in ("user_id", "user", "customer_id", "account_id", "subject"):
        assert identifier not in params, (
            f"EmbeddingsClient.embed gained {identifier!r} — the provider would "
            "be able to correlate a user's embeddings"
        )
    # Precondition: we are inspecting the method we think we are.
    assert {"model", "inputs"} <= params
