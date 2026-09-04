"""Authentication-infrastructure routes only.

Deliberately not a resource router: no project/upload/job/pipeline data is
touched here. ``/me`` exists solely to exercise ``get_current_user`` over a
real HTTP request (token verification, 401s, first-login provisioning), so
this step's authentication plumbing is provably wired end-to-end.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from edutoon.api.dependencies import get_current_user
from edutoon.repositories.users import UserRecord

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me")
async def get_me(user: Annotated[UserRecord, Depends(get_current_user)]) -> dict[str, str]:
    return {"id": str(user.id), "email": user.email}
