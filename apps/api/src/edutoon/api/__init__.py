"""API routers.

The ``/v1`` router is the mount point for all versioned endpoints.
"""

from fastapi import APIRouter

from edutoon.api.routers.auth import router as auth_router
from edutoon.api.routers.projects import router as projects_router

v1_router = APIRouter(prefix="/v1")
v1_router.include_router(auth_router)
v1_router.include_router(projects_router)
