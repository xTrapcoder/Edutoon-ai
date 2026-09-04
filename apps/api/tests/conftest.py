"""Test configuration.

Populates the required environment variables before the application is
imported so that settings resolution succeeds without a real ``.env`` file.
"""

from __future__ import annotations

import os

_TEST_ENV = {
    "ENVIRONMENT": "test",
    "DATABASE_URL": "postgresql+asyncpg://edutoon:edutoon@localhost:5433/edutoon",
    "DATABASE_DIRECT_URL": "postgresql+asyncpg://edutoon:edutoon@localhost:5433/edutoon",
    "REDIS_URL": "redis://localhost:6379/0",
    "STORAGE_ENDPOINT_URL": "http://localhost:9000",
    "STORAGE_ACCESS_KEY_ID": "edutoon",
    "STORAGE_SECRET_ACCESS_KEY": "edutoon123",
    "BUCKET_UPLOADS": "edutoon-uploads",
    "BUCKET_ASSETS": "edutoon-assets",
    "BUCKET_SEGMENTS": "edutoon-segments",
    "BUCKET_OUTPUTS": "edutoon-outputs",
}

for key, value in _TEST_ENV.items():
    os.environ.setdefault(key, value)
