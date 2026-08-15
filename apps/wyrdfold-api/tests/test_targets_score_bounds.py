"""Score-scale bounds on the target preference models.

Job scores are hard-clamped to 0-100 at the write site
(``services/scoring.py`` — ``score = max(0, min(100, ...))``, twice) and
``JobFitResult.fit_score`` is ``Field(ge=0, le=100)``. The preference models
allowed up to 200, so a user could save a cutoff of 150 and see the whole list
silently go empty with nothing explaining why. The editor accepted it, the
toast said "Preferences saved", and the panel badge flipped to "Custom".
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.targets import (
    NotificationThresholdsUpdate,
    TargetPreferences,
    TargetPreferencesUpdate,
)


class TestWriteBounds:
    """Writes are rejected outright — nothing legacy to accommodate."""

    @pytest.mark.parametrize("value", [0, 40, 99, 100])
    def test_accepts_the_real_range(self, value: int) -> None:
        assert TargetPreferencesUpdate(pref_score_cutoff=value).pref_score_cutoff == value

    @pytest.mark.parametrize("value", [101, 150, 200, 201, 1000])
    def test_rejects_a_cutoff_no_job_can_ever_reach(self, value: int) -> None:
        with pytest.raises(ValidationError):
            TargetPreferencesUpdate(pref_score_cutoff=value)

    def test_rejects_a_negative_cutoff(self) -> None:
        with pytest.raises(ValidationError):
            TargetPreferencesUpdate(pref_score_cutoff=-1)

    @pytest.mark.parametrize("value", [101, 150, 200])
    def test_notification_thresholds_share_the_ceiling(self, value: int) -> None:
        """Same 0-100 scale; the editor already capped these at 100, but an API
        client could set 150 and simply never be alerted."""
        with pytest.raises(ValidationError):
            NotificationThresholdsUpdate(job_score_threshold=value)
        with pytest.raises(ValidationError):
            NotificationThresholdsUpdate(sms_score_threshold=value)

    @pytest.mark.parametrize("value", [0, 70, 100, None])
    def test_notification_thresholds_accept_the_real_range(self, value: int | None) -> None:
        assert NotificationThresholdsUpdate(job_score_threshold=value).job_score_threshold == value


class TestLegacyReadClamp:
    """Reads must NOT blow up on a row saved under the old 200 ceiling.

    Tightening the read model alone would 500 the preferences page for anyone
    holding a legacy value — turning a cosmetic bound fix into an outage for
    exactly the users it was meant to help.
    """

    @pytest.mark.parametrize(("stored", "expected"), [(101, 100), (150, 100), (200, 100)])
    def test_folds_a_legacy_out_of_range_cutoff_to_the_ceiling(
        self, stored: int, expected: int
    ) -> None:
        prefs = TargetPreferences(pref_score_cutoff=stored)
        assert prefs.pref_score_cutoff == expected

    @pytest.mark.parametrize("stored", [0, 40, 100])
    def test_leaves_in_range_values_untouched(self, stored: int) -> None:
        assert TargetPreferences(pref_score_cutoff=stored).pref_score_cutoff == stored

    def test_still_rejects_a_negative_stored_value(self) -> None:
        """The clamp is one-directional — it fixes the bound we moved, and does
        not become a blanket "coerce anything" that would mask real corruption."""
        with pytest.raises(ValidationError):
            TargetPreferences(pref_score_cutoff=-5)

    def test_does_not_coerce_a_bool(self) -> None:
        """`isinstance(True, int)` is True in Python; the clamp must not treat a
        stray boolean as a score."""
        with pytest.raises(ValidationError):
            TargetPreferences(pref_score_cutoff="not-a-number")  # type: ignore[arg-type]
