"""API routers.

The ``/v1`` router is the mount point for all versioned endpoints.
"""

from fastapi import APIRouter

from edutoon.api.routers.auth import router as auth_router
from edutoon.api.routers.claim_verifications import router as claim_verifications_router
from edutoon.api.routers.jobs import router as jobs_router
from edutoon.api.routers.projects import router as projects_router
from edutoon.api.routers.script_audio import router as script_audio_router
from edutoon.api.routers.scripts import router as scripts_router
from edutoon.api.routers.storyboard import router as storyboard_router
from edutoon.api.routers.topics import router as topics_router
from edutoon.api.routers.uploaded_sources import router as uploaded_sources_router

v1_router = APIRouter(prefix="/v1")
v1_router.include_router(auth_router)
v1_router.include_router(projects_router)
v1_router.include_router(uploaded_sources_router)
v1_router.include_router(topics_router)
v1_router.include_router(claim_verifications_router)
v1_router.include_router(scripts_router)
v1_router.include_router(script_audio_router)
v1_router.include_router(storyboard_router)
v1_router.include_router(jobs_router)
