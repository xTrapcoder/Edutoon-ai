"""Authentication-infrastructure routes only.

Deliberately not a resource router: no project/upload/job/pipeline data is
touched here. ``/me`` exercises ``get_current_user`` over a real HTTP request
(token verification, 401s, the 403s for an uninvited or suspended account) and
tells the front end who the signed-in person is and which role the database
gives them - the only place a client learns its role, and never a place it can
set one.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from edutoon.api.policies import require_authenticated
from edutoon.api.schemas.admin_users import MeResponse
from edutoon.services.users import UserRecord

# Any active user, whatever the role: this is how a client finds out which one it is.
router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(require_authenticated)])


@router.get("/me")
async def get_me(user: Annotated[UserRecord, Depends(require_authenticated)]) -> MeResponse:
    return MeResponse.from_record(user)
