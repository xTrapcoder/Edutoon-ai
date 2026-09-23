from __future__ import annotations

from edutoon.core.context import bind_request_id, get_request_id


def test_get_request_id_defaults_to_none():
    assert get_request_id() is None


def test_bind_request_id_is_scoped_and_resets():
    with bind_request_id("req-123"):
        assert get_request_id() == "req-123"

    assert get_request_id() is None
