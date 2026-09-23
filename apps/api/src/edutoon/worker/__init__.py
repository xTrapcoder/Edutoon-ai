"""The background job worker.

Orchestration only (CLAUDE.md rule 1-2 in spirit, extended to this process):
``repositories.jobs.claim_next`` does the concurrency-safe claim,
``worker.dispatch`` maps a claimed job's ``kind`` to the existing
``services.*.run_*_job`` function, and ``worker.runner`` drives the
poll/claim/dispatch loop. No business logic lives here - everything a
handler does is whatever the corresponding service module already does.
"""
