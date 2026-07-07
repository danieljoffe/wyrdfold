"""The poll's source fan-out stays bounded to keep the DB-write herd small.

The write-concurrency machinery itself (``db_to_thread`` + the per-loop
semaphore + ``DB_WRITE_CONCURRENCY``) moved to ``app.services.db_write`` — its
tests live in ``test_db_write.py``. What's poller-specific, and guarded here,
is that ``POLL_CONCURRENCY`` stays low: fewer concurrent sources = a smaller
simultaneous write burst against the shared client.
"""

from __future__ import annotations

from app.services import db_write, poller


def test_poll_concurrency_is_bounded_below_legacy() -> None:
    """Source fan-out was lowered from 10 to reduce the herd; guard the intent
    so a future bump is a deliberate edit, not an accident."""
    assert poller.POLL_CONCURRENCY <= 8
    assert db_write.DB_WRITE_CONCURRENCY >= 1
