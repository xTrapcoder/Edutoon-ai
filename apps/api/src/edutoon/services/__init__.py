"""Service layer: the only thing routers are allowed to import (rule 2).

Each module wraps the matching ``repositories`` module — translating a
missing row into :class:`~edutoon.core.errors.NotFoundError` (rule 9) — and
holds nothing else yet. No business rules exist in Phase 2; this is purely
the seam routers will call through once endpoints are added.
"""
