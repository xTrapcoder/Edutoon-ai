"""Request/response schemas for ``/v1/auth/me`` and ``/v1/admin`` (users and invitations).

``role`` and ``status`` are ``Literal`` types mirroring migrations 0019 and 0002, so an invalid
value is a 422 here rather than a constraint violation. Request bodies reject unknown fields
(rule 10): a client cannot slip a ``role`` into a request that does not take one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from edutoon.domain.roles import AssignableStatus, InvitationStatus, Role, normalise_email
from edutoon.services.invitations import InvitationCreated, InvitationRecord
from edutoon.services.users import UserRecord

EMAIL_MAX_CHARS = 320


class MeResponse(BaseModel):
    id: UUID
    email: str
    display_name: str | None
    role: Role

    @classmethod
    def from_record(cls, user: UserRecord) -> MeResponse:
        return cls(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,  # type: ignore[arg-type]
        )


class UserResponse(BaseModel):
    id: UUID
    email: str
    display_name: str | None
    role: Role
    status: Literal["active", "suspended", "deleted"]
    has_signed_in: bool
    created_at: datetime

    @classmethod
    def from_record(cls, user: UserRecord) -> UserResponse:
        return cls(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,  # type: ignore[arg-type]
            status=user.status,  # type: ignore[arg-type]
            has_signed_in=user.clerk_user_id is not None,
            created_at=user.created_at,
        )


class UserListResponse(BaseModel):
    items: list[UserResponse]
    next_cursor: str | None


class UserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    status: AssignableStatus | None = None

    @model_validator(mode="after")
    def _something_to_change(self) -> UserUpdateRequest:
        if self.role is None and self.status is None:
            raise ValueError("send at least one of 'role' or 'status'")
        return self


class InvitationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=EMAIL_MAX_CHARS)
    role: Role = "employee"
    display_name: str | None = Field(default=None, max_length=200)

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value: str) -> str:
        email = normalise_email(value)
        # The same shape the database enforces (ck_invitations_email_shape), plus no whitespace.
        if "@" not in email[1:] or len(email) < 3 or any(ch.isspace() for ch in email):
            raise ValueError("must be an email address")
        return email

    @field_validator("display_name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip()


class InvitationResponse(BaseModel):
    id: UUID
    email: str
    display_name: str | None
    role: Role
    status: InvitationStatus
    expires_at: datetime
    accepted_at: datetime | None
    created_at: datetime

    @classmethod
    def from_record(cls, record: InvitationRecord) -> InvitationResponse:
        return cls(
            id=record.id,
            email=record.email,
            display_name=record.display_name,
            role=record.role,  # type: ignore[arg-type]
            status=record.status,  # type: ignore[arg-type]
            expires_at=record.expires_at,
            accepted_at=record.accepted_at,
            created_at=record.created_at,
        )


class InvitationCreatedResponse(InvitationResponse):
    """``delivery``: ``sent`` (Clerk was asked to email it), ``not_configured`` (no
    ``CLERK_SECRET_KEY`` - pass the sign-up link on yourself) or ``failed`` (Clerk could not be
    reached or refused; the invitation exists - revoke and re-create it to try again)."""

    delivery: Literal["sent", "not_configured", "failed"]

    @classmethod
    def from_created(cls, created: InvitationCreated) -> InvitationCreatedResponse:
        base = InvitationResponse.from_record(created.invitation)
        return cls(**base.model_dump(), delivery=created.delivery)


class InvitationListResponse(BaseModel):
    items: list[InvitationResponse]
    next_cursor: str | None
