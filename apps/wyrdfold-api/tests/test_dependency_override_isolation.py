"""``app.dependency_overrides`` never leaks across a test boundary (conftest fixture).

The two tests are order-dependent on purpose: the first installs an override
and deliberately does not clean it up; the second asserts it is gone. Without
the autouse fixture in ``tests/conftest.py`` the second test fails, which is
exactly the class of silent cross-test contamination the fixture exists to
stop.
"""

from __future__ import annotations

from app.dependencies import get_current_user_id
from app.main import app


def _leaked_user_id() -> str:
    return "leaked-user"


def test_a_test_that_forgets_to_clean_up_its_override() -> None:
    app.dependency_overrides[get_current_user_id] = _leaked_user_id
    assert app.dependency_overrides[get_current_user_id] is _leaked_user_id


def test_the_next_test_does_not_inherit_it() -> None:
    assert get_current_user_id not in app.dependency_overrides
