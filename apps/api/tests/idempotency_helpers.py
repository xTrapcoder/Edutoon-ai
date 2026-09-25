"""What the tests that check a route's idempotency contract share.

Every mutating route dedupes through ``services/idempotency.py`` and must (a)
commit its database transaction *before* the result is cached as completed or
returned, and (b) refuse an ``Idempotency-Key`` that is empty or over 255
characters with a 422 instead of letting it reach Postgres. Testing that for each
router needs the same three things, kept here so they are written once:

- :func:`idempotency_status` reads the dedupe record a request left in Redis;
- :data:`INVALID_KEYS` is the set of keys that must be refused;
- the ``redis_client`` fixture (``conftest.py``) gives a test its own connection.

To watch the commit, a test replaces ``db_session.commit`` with a spy that
records :func:`idempotency_status` at the moment it is called: it must still be
``"in_progress"`` there, and ``"completed"`` only after the response.
"""

from __future__ import annotations

import json

from edutoon.providers.cache import Redis

# Empty; one over the limit; and long enough to break the jobs unique index (the
# btree refuses an entry over ~2.7 kB, which used to surface as a 500).
INVALID_KEYS = ["", "k" * 256, "k" * 3_000]


async def idempotency_status(redis_client: Redis, path: str, key: str) -> str | None:
    """The status ("in_progress" or "completed") of the dedupe record ``key``
    left for ``path`` - whichever owner made it, since a test's key is a fresh
    UUID - or ``None`` if there is none (never made, or released after a failure).
    """
    matches = [name async for name in redis_client.scan_iter(match=f"idempotency:*:{path}:{key}")]
    if not matches:
        return None
    [name] = matches
    raw = await redis_client.get(name)
    return None if raw is None else str(json.loads(raw)["status"])
