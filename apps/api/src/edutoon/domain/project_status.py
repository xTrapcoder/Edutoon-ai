"""A project's status: the vocabulary, and the one rule that decides it.

``projects.status`` is a roll-up of where the project's pipeline has got to. Nobody sets it
by hand - the API refuses a ``status`` in a request - it follows the work the pipeline does
for the project, and this module is the single place that says how:

==============  ============================================================================
``draft``       Nothing has run yet. A project starts here.
``processing``  Background work is in flight: a job is ``queued`` or ``running``.
``ready``       Nothing is in flight and the most recent outcome was a success, so the
                project is ready for whatever comes next.
``failed``      Nothing is in flight and the most recent outcome was a failure: a job ended
                ``failed``, or an uploaded PDF could not be read.
``archived``    Set aside by its owner. The pipeline never moves a project out of it.
==============  ============================================================================

**It describes the latest outcome, so it changes as the work does.** A ``failed`` project
becomes ``processing`` when it is retried and ``ready`` when the retry succeeds; a ``ready``
one becomes ``processing`` the moment more work is queued. "Latest" is the most recently
*finished* thing - a job's ``finished_at``, or the time a PDF failed to parse - so a slow job
that ends after a fast one is the outcome that counts. A ``cancelled`` job is neither: it
was never an outcome.

The rule is deliberately blind to *which* job it is (``build_evidence``, ``extract_topics``,
``generate_script`` and any kind added later all count alike), so a new pipeline stage needs
no change here. It also says nothing about what is stored - the script and the topics are
their own records with their own statuses; this is only the pipeline's state.

The values mirror ``ck_projects_status`` (migration 0002); the migration stays the source of
truth for what the database accepts, and this module is how the code names those values.
"""

from __future__ import annotations

from typing import Final, Literal

ProjectStatus = Literal["draft", "processing", "ready", "failed", "archived"]

STATUS_DRAFT: Final[ProjectStatus] = "draft"
STATUS_PROCESSING: Final[ProjectStatus] = "processing"
STATUS_READY: Final[ProjectStatus] = "ready"
STATUS_FAILED: Final[ProjectStatus] = "failed"
STATUS_ARCHIVED: Final[ProjectStatus] = "archived"

Outcome = Literal["succeeded", "failed"]
"""How the most recent piece of finished pipeline work ended."""


def derive_project_status(
    *,
    current: ProjectStatus,
    active_jobs: int,
    latest_outcome: Outcome | None,
) -> ProjectStatus:
    """The status a project should have, given its pipeline's state.

    ``active_jobs`` is how many of its jobs are ``queued`` or ``running``;
    ``latest_outcome`` is how the most recently finished work ended, or ``None`` if nothing
    has finished. ``current`` matters only for ``archived``, which is never overwritten.
    """
    if current == STATUS_ARCHIVED:
        return STATUS_ARCHIVED
    if active_jobs > 0:
        return STATUS_PROCESSING
    if latest_outcome == "succeeded":
        return STATUS_READY
    if latest_outcome == "failed":
        return STATUS_FAILED
    return STATUS_DRAFT
