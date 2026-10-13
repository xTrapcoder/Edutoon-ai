"""``providers/clerk.py``: the ``email_verified`` claim and the invitation request.

No network: ``_post_invitation`` (the one function that opens a socket) is replaced. That the
request shape is accepted by a real Clerk instance is NOT verified - the keys are placeholders.
"""

from __future__ import annotations

import urllib.error
from types import SimpleNamespace

import pytest

from edutoon.providers import clerk


def test_email_verified_is_true_only_when_the_claim_is_literally_true(mint_token):
    assert clerk.verify_token(mint_token(email_verified=True)).email_verified is True
    assert clerk.verify_token(mint_token(email_verified=False)).email_verified is False
    assert clerk.verify_token(mint_token(email_verified=None)).email_verified is False  # absent
    assert clerk.verify_token(mint_token(email_verified="true")).email_verified is False  # a string


def _settings(**overrides):
    base = {
        "CLERK_SECRET_KEY": "sk_test_not_real",
        "CLERK_API_BASE_URL": "https://api.clerk.example/",
        "CLERK_INVITATION_REDIRECT_URL": None,
        "APP_BASE_URL": "http://localhost:3000/",
    }
    return SimpleNamespace(**{**base, **overrides})


async def test_send_invitation_posts_the_address_and_redirect_and_no_role(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    def fake_post(url, secret_key, payload):
        calls.append((url, secret_key, payload))
        return {"id": "inv_abc"}

    monkeypatch.setattr(clerk, "get_settings", lambda: _settings())
    monkeypatch.setattr(clerk, "_post_invitation", fake_post)

    invitation_id = await clerk.send_invitation(email="a@x.co")

    assert invitation_id == "inv_abc"
    [(url, key, payload)] = calls
    assert url == "https://api.clerk.example/v1/invitations"
    assert key == "sk_test_not_real"
    assert payload == {
        "email_address": "a@x.co",
        "redirect_url": "http://localhost:3000/sign-up",
        "ignore_existing": True,
    }


async def test_without_a_secret_key_nothing_is_sent(monkeypatch):
    monkeypatch.setattr(clerk, "get_settings", lambda: _settings(CLERK_SECRET_KEY=None))
    monkeypatch.setattr(clerk, "_post_invitation", lambda *a: pytest.fail("must not be called"))

    assert clerk.invitations_configured() is False
    with pytest.raises(clerk.ClerkInvitationError):
        await clerk.send_invitation(email="a@x.co")


async def test_a_response_without_an_id_is_an_error(monkeypatch):
    monkeypatch.setattr(clerk, "get_settings", lambda: _settings())
    monkeypatch.setattr(clerk, "_post_invitation", lambda *a: {"object": "invitation"})

    with pytest.raises(clerk.ClerkInvitationError):
        await clerk.send_invitation(email="a@x.co")


def test_http_errors_report_the_status_only_never_the_body_or_the_key(monkeypatch):
    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 422, "bad", {}, None)

    monkeypatch.setattr(clerk.urllib.request, "urlopen", refuse)

    with pytest.raises(clerk.ClerkInvitationError) as raised:
        clerk._post_invitation("https://api.clerk.example/v1/invitations", "sk_secret_value", {})

    assert "422" in str(raised.value)
    assert "sk_secret_value" not in str(raised.value)


def test_network_failures_become_invitation_errors(monkeypatch):
    def unreachable(request, timeout):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(clerk.urllib.request, "urlopen", unreachable)

    with pytest.raises(clerk.ClerkInvitationError):
        clerk._post_invitation("https://api.clerk.example/v1/invitations", "sk", {})
