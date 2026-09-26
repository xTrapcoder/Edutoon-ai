"""Write the API's OpenAPI document to ``packages/contracts/openapi.json``.

The front end generates its TypeScript types from that file (``pnpm contracts:generate``),
so the API stays the single source of truth for every request and response shape.

    uv run python scripts/export_openapi.py           # (re)write the file
    uv run python scripts/export_openapi.py --check   # exit 1 if the file is stale

Nothing here connects to Postgres, Redis, object storage, Clerk or a model provider: it
builds the FastAPI application object and asks it to describe itself, without running its
lifespan. Settings are still validated when the app is built, so any required variable that
is not already set gets a placeholder for this process only - a real ``.env`` or exported
value always wins, and nothing is written anywhere but the contract file.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Placeholders for the settings that have no default. The values never leave this process
# and are never used to connect to anything.
_PLACEHOLDER_ENVIRONMENT = {
    "ENVIRONMENT": "development",
    "DATABASE_URL": "postgresql+asyncpg://placeholder:placeholder@localhost:5433/placeholder",
    "DATABASE_DIRECT_URL": "postgresql+asyncpg://placeholder:placeholder@localhost:5433/placeholder",
    "REDIS_URL": "redis://localhost:6379/0",
    "STORAGE_ENDPOINT_URL": "http://localhost:9000",
    "STORAGE_ACCESS_KEY_ID": "placeholder",
    "STORAGE_SECRET_ACCESS_KEY": "placeholder",
    "BUCKET_UPLOADS": "placeholder-uploads",
    "BUCKET_ASSETS": "placeholder-assets",
    "BUCKET_SEGMENTS": "placeholder-segments",
    "BUCKET_OUTPUTS": "placeholder-outputs",
    "CLERK_JWKS_URL": "https://placeholder.invalid/.well-known/jwks.json",
    "CLERK_ISSUER": "https://placeholder.invalid",
    "OPENAI_API_KEY": "placeholder",
    "ANTHROPIC_API_KEY": "placeholder",
}

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = _REPOSITORY_ROOT / "packages" / "contracts" / "openapi.json"


def build_document() -> str:
    for name, value in _PLACEHOLDER_ENVIRONMENT.items():
        os.environ.setdefault(name, value)

    from edutoon.main import create_app  # imported late: it reads the settings above

    document = create_app().openapi()
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    rendered = build_document()

    if check:
        current = CONTRACT_PATH.read_text(encoding="utf-8") if CONTRACT_PATH.exists() else ""
        if current.replace("\r\n", "\n") == rendered:
            print(f"{CONTRACT_PATH.relative_to(_REPOSITORY_ROOT)} is up to date.")
            return 0
        print(
            f"{CONTRACT_PATH.relative_to(_REPOSITORY_ROOT)} is out of date. "
            "Run `make contracts` and commit the result.",
            file=sys.stderr,
        )
        return 1

    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" keeps the file identical on every platform, so --check is stable.
    with CONTRACT_PATH.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
    print(f"Wrote {CONTRACT_PATH.relative_to(_REPOSITORY_ROOT)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
