"""Redis client construction - the only module allowed to import the redis
SDK (rule 3). Everything else (idempotency, future caching/queueing) goes
through the ``Redis`` type/handle exported here.
"""

from __future__ import annotations

import redis.asyncio as redis

Redis = redis.Redis


def get_redis_client(url: str) -> Redis:
    return redis.from_url(url, decode_responses=True)
