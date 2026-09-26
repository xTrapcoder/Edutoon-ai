"""Check that the API and the web app are set up against the SAME Clerk instance.

    uv run python scripts/check_clerk.py                 # configuration only
    uv run python scripts/check_clerk.py --token-stdin   # ... and verify a session token too

It reads ``apps/api/.env`` (through the API's own settings) and ``apps/web/.env.local``, and
reports what is wrong in words. It never prints a secret: for keys it says only whether they are
set and what mode they are in (test or live); for a token, only whether the API would accept it
and which required claim is missing. The Clerk frontend host it prints is public.

With ``--token-stdin`` it verifies a session token exactly as the API does
(``edutoon.providers.clerk.verify_token``), so "the token is accepted" means the same thing here
as it does on a real request. Get a token in the signed-in app's browser console with
``await window.Clerk.session.getToken()`` and pipe it in; it is read from stdin, never from an
argument, so it does not land in a shell history.

Exits 0 if everything checked passed, 1 otherwise. See "Clerk Local Setup" in apps/api/README.md.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_WEB_ENV = _REPOSITORY_ROOT / "apps" / "web" / ".env.local"

_PLACEHOLDER_WORDS = ("your-", "placeholder", "example", "changeme", "xxxx")

failures = 0


def report(ok: bool, message: str) -> None:
    global failures
    if not ok:
        failures += 1
    print(f"  [{'ok' if ok else 'FAIL'}] {message}")


def looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return not value or any(word in lowered for word in _PLACEHOLDER_WORDS)


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip("'\"")
    return values


def frontend_host_from_publishable_key(key: str) -> tuple[str | None, str | None]:
    """``pk_test_<base64>`` encodes ``<frontend api host>$``. Returns (mode, host)."""
    match = re.fullmatch(r"pk_(test|live)_([A-Za-z0-9+/=_-]+)", key)
    if not match:
        return None, None
    mode, encoded = match.groups()
    try:
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return mode, None
    host = decoded.rstrip("$")
    return mode, host if re.fullmatch(r"[A-Za-z0-9.-]+", host) else None


def check_api() -> tuple[str, str] | None:
    print("API (apps/api/.env)")
    try:
        from edutoon.core.config import get_settings

        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - say what is wrong, briefly, without a traceback
        report(False, f"the API's settings do not load: {type(exc).__name__}")
        return None

    issuer, jwks = settings.CLERK_ISSUER, settings.CLERK_JWKS_URL
    report(not looks_like_placeholder(issuer), "CLERK_ISSUER is set to a real value")
    report(issuer.startswith("https://"), "CLERK_ISSUER is an https:// URL")
    report(
        jwks == issuer.rstrip("/") + "/.well-known/jwks.json",
        "CLERK_JWKS_URL is the issuer plus /.well-known/jwks.json",
    )
    if looks_like_placeholder(issuer) or not issuer.startswith("https://"):
        return None
    return issuer, jwks


def check_jwks(jwks_url: str) -> None:
    try:
        with urllib.request.urlopen(jwks_url, timeout=10) as response:  # noqa: S310 - https, checked
            keys = json.load(response).get("keys", [])
        report(len(keys) > 0, f"the JWKS URL is reachable and lists {len(keys)} signing key(s)")
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        report(False, f"the JWKS URL could not be read ({type(exc).__name__})")


def check_web(issuer: str | None) -> None:
    print("Web (apps/web/.env.local)")
    if not _WEB_ENV.exists():
        report(False, "apps/web/.env.local does not exist (copy .env.local.example)")
        return
    env = read_env_file(_WEB_ENV)

    publishable = env.get("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "")
    secret = env.get("CLERK_SECRET_KEY", "")
    report(bool(publishable), "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY is set")
    report(bool(secret), "CLERK_SECRET_KEY is set (its value is never shown)")
    report(
        env.get("NEXT_PUBLIC_CLERK_KEYLESS_DISABLED") == "1",
        "keyless mode is off (NEXT_PUBLIC_CLERK_KEYLESS_DISABLED=1)",
    )

    if publishable:
        mode, host = frontend_host_from_publishable_key(publishable)
        report(mode is not None and host is not None, "the publishable key has a valid shape")
        if mode and secret:
            report(
                secret.startswith(f"sk_{mode}_"),
                f"the secret key is in the same mode as the publishable key ({mode})",
            )
        if host and issuer:
            report(
                urlparse(issuer).hostname == host,
                f"the web app's Clerk instance ({host}) is the one the API trusts",
            )
        if mode == "live":
            print("  note: these are LIVE keys. Use a development instance while building.")

    api_url = env.get("NEXT_PUBLIC_API_BASE_URL", "")
    report(bool(api_url), "NEXT_PUBLIC_API_BASE_URL is set")


def check_token(token: str) -> None:
    print("Session token")
    token = token.strip()
    if token.count(".") != 2:
        report(False, "that does not look like a JWT (three dot-separated parts)")
        return
    try:
        from edutoon.providers.clerk import ClerkTokenError, verify_token

        claims = verify_token(token)
    except ClerkTokenError as exc:
        # The library's message names what failed (expired, wrong issuer, a missing claim);
        # it never contains the token itself.
        report(False, f"the API would REJECT this token: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        report(False, f"the token could not be checked ({type(exc).__name__})")
        return
    report(True, "the API accepts this token (signature, issuer, expiry, sub and email present)")
    report(bool(claims.clerk_user_id), "it identifies a user (sub)")
    report(bool(claims.email), "it carries an email claim (the API needs it to create the user)")


def main(argv: list[str]) -> int:
    issuer_and_jwks = check_api()
    if issuer_and_jwks and "--no-network" not in argv:
        check_jwks(issuer_and_jwks[1])
    check_web(issuer_and_jwks[0] if issuer_and_jwks else None)
    if "--token-stdin" in argv:
        check_token(sys.stdin.read())
    print()
    print("Everything checked passed." if failures == 0 else f"{failures} problem(s) to fix.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
