"""Mint a short-lived Clerk session token for one user of the *development* Clerk instance.

For manual walkthroughs against the real API with real Clerk verification (no stand-in). It prints
**only the token** to stdout (everything else goes to stderr), so it can be captured without being
displayed:

    export ADMIN_TOKEN=$(uv run python scripts/dev_clerk_token.py --email you@example.com)

How: a one-minute Clerk sign-in ticket is exchanged for a session through the Frontend API, a
session token is minted, and the session is revoked straight away. The token itself lives about a
minute, so mint a fresh one per batch of requests.

Safety: refuses to run unless the secret key is a *test* key (``sk_test_``) and the environment is
not production. It reads the Clerk keys from ``apps/web/.env.local`` and never prints them.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

import httpx

API = "https://api.clerk.com/v1"
WEB_ENV = Path(__file__).resolve().parents[2] / "web" / ".env.local"


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _fail(message: str) -> None:
    print(f"dev_clerk_token: {message}", file=sys.stderr)
    raise SystemExit(1)


def mint(email: str | None = None, user_id: str | None = None) -> str:
    """A fresh session token for the Clerk user with this address (or id)."""
    if os.environ.get("ENVIRONMENT", "development") == "production":
        _fail("refusing to run in production")
    env = _read_env(WEB_ENV)
    secret = env.get("CLERK_SECRET_KEY", "")
    publishable = env.get("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "")
    if not secret.startswith("sk_test_") or not publishable.startswith("pk_test_"):
        _fail("only a development (sk_test_/pk_test_) Clerk instance is supported")
    frontend = "https://" + base64.b64decode(publishable.split("_", 2)[-1] + "==").decode().rstrip(
        "$"
    )
    headers = {"Authorization": f"Bearer {secret}"}

    with httpx.Client(timeout=30, headers=headers) as backend:
        if user_id is None:
            found = backend.get(f"{API}/users", params={"email_address": [email]})
            users = found.json() if found.status_code == 200 else []
            if len(users) != 1:
                _fail(f"expected exactly one Clerk user with that address, found {len(users)}")
            user_id = users[0]["id"]
        ticket = backend.post(
            f"{API}/sign_in_tokens", json={"user_id": user_id, "expires_in_seconds": 60}
        )
        if ticket.status_code != 200:
            _fail(f"Clerk refused the sign-in ticket (HTTP {ticket.status_code})")
        session_id: str | None = None
        try:
            with httpx.Client(timeout=30, base_url=frontend) as front:
                dev_browser = front.post("/v1/dev_browser")
                params = {}
                if dev_browser.status_code == 200 and dev_browser.json().get("token"):
                    params["__clerk_db_jwt"] = dev_browser.json()["token"]
                signed_in = front.post(
                    "/v1/client/sign_ins",
                    params=params,
                    data={"strategy": "ticket", "ticket": ticket.json()["token"]},
                )
                if signed_in.status_code != 200:
                    _fail(f"ticket sign-in failed (HTTP {signed_in.status_code})")
                session_id = (signed_in.json().get("response") or {}).get("created_session_id")
                if not session_id:
                    _fail("sign-in did not complete (the account may need another step)")
                minted = front.post(f"/v1/client/sessions/{session_id}/tokens", params=params)
                if minted.status_code != 200 or not minted.json().get("jwt"):
                    _fail(f"token request failed (HTTP {minted.status_code})")
                return str(minted.json()["jwt"])
        finally:
            if session_id:
                backend.post(f"{API}/sessions/{session_id}/revoke")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--email", help="the user's email address in the development Clerk instance")
    who.add_argument("--user-id", help="the user's Clerk id (user_...)")
    args = parser.parse_args()
    sys.stdout.write(mint(email=args.email, user_id=args.user_id))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
