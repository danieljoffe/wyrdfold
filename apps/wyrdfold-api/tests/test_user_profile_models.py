"""Tests for user_profile Pydantic models — focuses on the E.164 phone
validator (F5-C). The router is a thin wrapper over Supabase; the validator
is the only piece with non-trivial logic worth pinning down."""

import pytest
from pydantic import ValidationError

from app.models.user_profile import (
    IdentityFieldsUpdate,
    NotificationPreferencesUpdate,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Already E.164 (with permissive formatting).
        ("+14155552671", "+14155552671"),
        ("+1 415 555 2671", "+14155552671"),
        ("+1 (415) 555-2671", "+14155552671"),
        ("  +442071838750  ", "+442071838750"),
        # Human input we now format ON THE USER'S BEHALF (US default): a bare
        # 10-digit number, common punctuation, and 11 digits led by the '1'
        # country code — none of these used to be accepted (they 422'd and,
        # because the PATCH is atomic, silently dropped every other field).
        ("415-555-2671", "+14155552671"),
        ("(415) 555-2671", "+14155552671"),
        ("4155552671", "+14155552671"),
        ("415.555.2671", "+14155552671"),
        ("1 (415) 555-2671", "+14155552671"),
        ("1-415-555-2671", "+14155552671"),
        # Blank / None clears the field.
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_phone_normalization_accepts_valid(raw: str | None, expected: str | None) -> None:
    """Human phone input normalizes to E.164 (US default); blank/None clears."""
    model = NotificationPreferencesUpdate(phone_number=raw)
    assert model.phone_number == expected
    identity = IdentityFieldsUpdate(phone_number=raw)
    assert identity.phone_number == expected


@pytest.mark.parametrize(
    "raw",
    [
        "+0123456789",  # leading zero in country code
        "+1",  # too short (only country code)
        "+",  # just plus
        "not a phone",  # no digits at all
        "12345",  # too few digits, no country code → can't resolve
        "555-2671",  # 7 digits (no area code) → not a resolvable US number
        "+12345678901234567890",  # too long (>15 digits)
    ],
)
def test_phone_validation_rejects_unresolvable(raw: str) -> None:
    with pytest.raises(ValidationError) as exc:
        NotificationPreferencesUpdate(phone_number=raw)
    assert "phone number" in str(exc.value).lower()

    with pytest.raises(ValidationError):
        IdentityFieldsUpdate(phone_number=raw)
