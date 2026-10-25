"""The job worker for UI QA - the same worker, on the QA database. DEVELOPMENT ONLY.

    uv run python scripts/dev_qa_worker.py

Start it beside ``dev_qa_api.py``; both use the isolated ``edutoon_qa`` database
(``_qa_database.py``). Everything else is exactly ``python -m edutoon.worker``.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from _qa_database import use_qa_database

from edutoon.core.config import get_settings


def main() -> None:
    if get_settings().ENVIRONMENT != "development":
        sys.exit("dev_qa_worker.py only runs with ENVIRONMENT=development.")
    use_qa_database()
    from edutoon.worker.__main__ import main as run_worker

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker())


if __name__ == "__main__":
    main()
