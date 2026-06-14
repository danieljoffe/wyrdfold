"""RLS gate for the per-user Storage buckets (#79 storage hardening).

Proves what mocks can't: an object uploaded under one user's `<uid>/`
folder is downloadable through that user's JWT-bound client, and a
different user's client is denied — Storage RLS (folder prefix =
auth.uid()) is the control. This also guards the `get_user_client` wiring
that binds the bearer onto the storage sub-client (not just postgrest);
if that regressed, a user couldn't even read their own object and these
fail.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from supabase import Client

pytestmark = pytest.mark.integration

_BUCKET = "tailored-resumes"


@pytest.fixture
def seeded_objects(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> Iterator[tuple[str, str, str, str]]:
    uid_a, uid_b = two_seeded_users
    path_a = f"{uid_a}/rls-int-test.docx"
    path_b = f"{uid_b}/rls-int-test.docx"
    opts = {"content-type": "application/octet-stream", "upsert": "true"}
    service_client.storage.from_(_BUCKET).upload(path=path_a, file=b"A", file_options=opts)
    service_client.storage.from_(_BUCKET).upload(path=path_b, file=b"B", file_options=opts)
    try:
        yield uid_a, path_a, uid_b, path_b
    finally:
        service_client.storage.from_(_BUCKET).remove([path_a, path_b])


def test_user_can_download_own_object(
    seeded_objects: tuple[str, str, str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, path_a, _uid_b, _path_b = seeded_objects
    client_a = user_client_factory(uid_a)
    assert client_a.storage.from_(_BUCKET).download(path_a) == b"A"


def test_user_cannot_download_another_users_object(
    seeded_objects: tuple[str, str, str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, _path_a, _uid_b, path_b = seeded_objects
    client_a = user_client_factory(uid_a)
    # RLS denies: the object isn't visible under A's auth.uid(), so the
    # storage API errors rather than returning B's bytes.
    with pytest.raises(Exception):  # any storage error is a pass
        client_a.storage.from_(_BUCKET).download(path_b)
