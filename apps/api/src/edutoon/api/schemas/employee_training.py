"""Response schemas for ``/v1/training`` - what an employee sees of their own courses.

Never the storage bucket or key, never another employee's data, and never a module the employee
cannot see at all (an unpublished one): a module is either listed - open or locked - or absent.
A locked module names the earlier module that blocks it, so the screen can say what to finish first.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from edutoon.services.training_access import BlockingModule, MyCourse, MyModule

LockReason = Literal["earlier_module_not_passed"]


class ModuleLockResponse(BaseModel):
    reason: LockReason
    blocking_module_id: UUID
    blocking_module_position: int
    blocking_module_title: str

    @classmethod
    def from_blocking(cls, blocking: BlockingModule) -> ModuleLockResponse:
        return cls(
            reason="earlier_module_not_passed",
            blocking_module_id=blocking.id,
            blocking_module_position=blocking.position,
            blocking_module_title=blocking.title,
        )


class MyModuleResponse(BaseModel):
    id: UUID
    position: int
    title: str
    description: str | None
    duration_ms: int
    width: int
    height: int
    locked: bool
    lock: ModuleLockResponse | None
    """``None`` when ``locked`` is false."""

    @classmethod
    def from_view(cls, view: MyModule) -> MyModuleResponse:
        module = view.module
        return cls(
            id=module.id,
            position=module.position,
            title=module.title,
            description=module.description,
            duration_ms=module.video_duration_ms,
            width=module.video_width,
            height=module.video_height,
            locked=view.locked,
            lock=ModuleLockResponse.from_blocking(view.blocking) if view.blocking else None,
        )


class MyCourseResponse(BaseModel):
    id: UUID
    title: str
    description: str | None
    modules: list[MyModuleResponse]

    @classmethod
    def from_view(cls, view: MyCourse) -> MyCourseResponse:
        return cls(
            id=view.course.id,
            title=view.course.title,
            description=view.course.description,
            modules=[MyModuleResponse.from_view(m) for m in view.modules],
        )


class MyCourseListResponse(BaseModel):
    items: list[MyCourseResponse]
