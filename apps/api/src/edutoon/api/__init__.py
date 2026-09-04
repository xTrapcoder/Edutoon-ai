"""API routers.

The ``/v1`` router is the mount point for all versioned endpoints. No routes
are registered yet — Phase 1 only wires the prefix.
"""

from fastapi import APIRouter

v1_router = APIRouter(prefix="/v1")
