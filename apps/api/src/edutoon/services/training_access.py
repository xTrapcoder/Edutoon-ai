"""Training module access: the seam to Step 3's assessment results.

``decide_module_access`` (``domain/training_access.py``) needs to know which modules an employee has
*passed*. In Step 2 nothing can be passed - assessments do not exist - so this is the one place that
answers, and it answers **nobody has passed anything**.

That is the fail-closed choice, not a placeholder that lets people through: with an empty set the
first published module of a course is open to an enrolled employee (it has no predecessor) and every
later module is locked, for everyone, until Step 3 replaces :func:`passed_module_ids` with a real
query over submitted, passing attempts. Nothing else changes when it does - the rule, the routes and
their tests already go through this function.

Deliberately the *only* thing this module does in Piece 1. Loading the facts and enforcing the rule
on a request (``require_module_access``) arrives with the employee routes in Piece 3.
"""

from __future__ import annotations

from collections.abc import Collection
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


async def passed_module_ids(
    session: AsyncSession, *, user_id: UUID, module_ids: Collection[UUID]
) -> frozenset[UUID]:
    """The subset of ``module_ids`` that ``user_id`` has passed.

    Step 2: always empty - no assessment, so no pass can exist, so no module is ever considered
    passed. **Step 3 replaces this body** with a query for a submitted attempt with ``passed``.
    It must keep failing closed: on any doubt, return fewer ids, never more.
    """
    return frozenset()
