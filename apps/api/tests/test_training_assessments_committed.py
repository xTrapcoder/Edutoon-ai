# ruff: noqa: F811 - pytest fixtures imported from test_training_committed
"""Publishing, unpublishing and submitting an assessment against a database that really commits,
with transactions that really overlap.

The rolled-back tests cannot show what two simultaneous transactions do to each other. The property
at stake: **an assessment that has an attempt is never a draft** - an unpublish racing a submission
must be refused if the submission got in first, and the submission must find a draft (a 404) if the
unpublish did. That rests on one lock order in ``services.assessments``: publish / unpublish / save
hold the course and then the module row ``FOR UPDATE`` before they look, and a submission holds the
module row ``FOR KEY SHARE`` first and reads the assessment after. The two conflict, and neither
waits while holding something the other needs, so they cannot deadlock either.

Two kinds of test, both on the scratch database of ``test_training_committed``'s ``world``:

* **interleavings made deterministic** - one transaction is held open (uncommitted) so the other is
  provably *blocked on the lock* (it is still pending after a pause), then released;
* **a stress race** through the real HTTP routes, checking the invariant after each round.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from edutoon.core.errors import ConflictError, NotFoundError
from edutoon.domain.training_assessment import Answer
from edutoon.repositories import assessments as assessments_repo
from edutoon.services import assessments as assessments_service
from tests.test_training_committed import BASE, World, world  # noqa: F401
from tests.training_api_helpers import assessment_payload

pytestmark = pytest.mark.real_auth

BLOCKED_FOR = 0.75
"""Seconds a blocked transaction is watched for. A statement that is not waiting on a lock finishes
in milliseconds, so a task still pending after this long is waiting."""


async def _course_with_module(world: World, client) -> tuple[str, str]:
    """A published course containing one published module the employee is enrolled in."""
    await world.fake_render()
    course = await world.make_course(client, f"Race {uuid4().hex[:6]}")
    module = await world.publish(client, course)
    assert module.status_code == 201, module.text
    published = await client.post(f"{BASE}/courses/{course}/publish", headers=world.h())
    assert published.status_code == 200, published.text
    enrolled = await client.post(
        f"{BASE}/courses/{course}/enrolments",
        json={"user_id": str(world.employee.user.id)},
        headers=world.h(),
    )
    assert enrolled.status_code == 201, enrolled.text
    return course, module.json()["id"]


async def _save(world: World, client, module_id: str, *, publish: bool) -> None:
    saved = await client.put(
        f"{BASE}/modules/{module_id}/assessment", json=assessment_payload(), headers=world.h()
    )
    assert saved.status_code == 200, saved.text
    if publish:
        live = await client.post(
            f"{BASE}/modules/{module_id}/assessment/publish", headers=world.h()
        )
        assert live.status_code == 200, live.text


async def _right_answers(world: World, module_id: str) -> list[Answer]:
    async with world.sessionmaker() as session:
        view = await assessments_service.get_admin_view(session, module_id=UUID(module_id))
    return [Answer(q.id, next(o.id for o in q.options if o.is_correct)) for q in view.questions]


async def _state(world: World, module_id: str) -> tuple[str, int]:
    """(status, attempts) of the module's assessment, read in a transaction of its own."""
    rows = await world.rows(
        "SELECT a.status, "
        "(SELECT count(*) FROM assessment_attempts t WHERE t.module_id = a.module_id) "
        "FROM assessments a WHERE a.module_id = :m",
        m=module_id,
    )
    [(status, attempts)] = rows
    return status, attempts


async def _in_own_transaction(world: World, work):
    """Run ``work(session)`` in a session of its own and commit; roll back and re-raise on error."""
    async with world.sessionmaker() as session:
        try:
            result = await work(session)
            await session.commit()
            return result
        except BaseException:
            await session.rollback()
            raise


def _unpublish(world: World, module_id: str):
    return _in_own_transaction(
        world,
        lambda s: assessments_service.unpublish_assessment(
            s, actor_id=world.admin.user.id, module_id=UUID(module_id)
        ),
    )


def _publish(world: World, module_id: str):
    return _in_own_transaction(
        world,
        lambda s: assessments_service.publish_assessment(
            s, actor_id=world.admin.user.id, module_id=UUID(module_id)
        ),
    )


async def _still_waiting(task: asyncio.Task) -> bool:
    done, _ = await asyncio.wait({task}, timeout=BLOCKED_FOR)
    return not done


async def test_an_unpublish_waits_for_a_submission_in_flight_and_is_then_refused(world):
    async with world.client() as client:
        _, module_id = await _course_with_module(world, client)
        await _save(world, client, module_id, publish=True)
    answers = await _right_answers(world, module_id)

    async with world.sessionmaker() as submitting:
        attempt = await assessments_service.submit_attempt(
            submitting,
            world.employee.user,
            module_id=UUID(module_id),
            answers=answers,
            idempotency_key=f"race-{uuid4()}",
        )  # graded and inserted, NOT committed: the assessment row is held FOR SHARE
        unpublishing = asyncio.create_task(_unpublish(world, module_id))

        assert await _still_waiting(unpublishing), "unpublish did not wait for the submission"
        await submitting.commit()

    with pytest.raises(ConflictError, match="has attempts"):
        await unpublishing
    assert attempt.passed is True
    assert await _state(world, module_id) == ("published", 1)  # never a draft with an attempt


async def test_a_submission_waits_for_an_unpublish_in_flight_and_then_finds_a_draft(world):
    async with world.client() as client:
        _, module_id = await _course_with_module(world, client)
        await _save(world, client, module_id, publish=True)
    answers = await _right_answers(world, module_id)

    async with world.sessionmaker() as unpublishing:
        await assessments_service.unpublish_assessment(
            unpublishing, actor_id=world.admin.user.id, module_id=UUID(module_id)
        )  # NOT committed: the assessment row is held FOR UPDATE
        submitting = asyncio.create_task(
            _in_own_transaction(
                world,
                lambda s: assessments_service.submit_attempt(
                    s,
                    world.employee.user,
                    module_id=UUID(module_id),
                    answers=answers,
                    idempotency_key=f"race-{uuid4()}",
                ),
            )
        )

        assert await _still_waiting(submitting), "the submission did not wait for the unpublish"
        await unpublishing.commit()

    with pytest.raises(NotFoundError):  # it re-read the row after the wait: a draft is "not found"
        await submitting
    assert await _state(world, module_id) == ("draft", 0)


async def test_two_publishes_at_once_publish_once_and_the_loser_is_a_conflict(world):
    async with world.client() as client:
        _, module_id = await _course_with_module(world, client)
        await _save(world, client, module_id, publish=False)

    async with world.sessionmaker() as first:
        result = await assessments_service.publish_assessment(
            first, actor_id=world.admin.user.id, module_id=UUID(module_id)
        )  # NOT committed
        second = asyncio.create_task(_publish(world, module_id))
        assert await _still_waiting(second), "the second publish did not wait"
        await first.commit()

    with pytest.raises(ConflictError, match="already published"):
        await second
    stamped = await world.scalar(
        "SELECT published_at FROM assessments WHERE module_id = :m", m=module_id
    )
    assert stamped == result.view.assessment.published_at  # the winner's stamp, untouched


async def test_a_save_racing_a_publish_cannot_edit_the_assessment_it_just_published(world):
    async with world.client() as client:
        _, module_id = await _course_with_module(world, client)
        await _save(world, client, module_id, publish=False)
    before = await world.rows(
        "SELECT count(*) FROM assessment_questions q JOIN assessments a ON a.id = q.assessment_id "
        "WHERE a.module_id = :m",
        m=module_id,
    )

    async with world.sessionmaker() as publishing:
        await assessments_service.publish_assessment(
            publishing, actor_id=world.admin.user.id, module_id=UUID(module_id)
        )  # NOT committed
        async with world.client(raise_app_exceptions=False) as client:
            saving = asyncio.create_task(
                client.put(
                    f"{BASE}/modules/{module_id}/assessment",
                    json=assessment_payload(questions=5),
                    headers=world.h(),
                )
            )
            assert await _still_waiting(saving), "the save did not wait for the publish"
            await publishing.commit()
            response = await saving

    assert response.status_code == 409, response.text
    after = await world.rows(
        "SELECT count(*) FROM assessment_questions q JOIN assessments a ON a.id = q.assessment_id "
        "WHERE a.module_id = :m",
        m=module_id,
    )
    assert after == before  # the published assessment still has its published questions
    assert (await _state(world, module_id))[0] == "published"


@pytest.mark.parametrize("round_number", range(6))
async def test_an_unpublish_racing_a_submission_never_leaves_a_draft_with_an_attempt(
    world, round_number
):
    """Through the real routes, fired together. Whichever wins, the pair of answers is one of two
    coherent ones - and in every case an assessment with an attempt is still published."""
    async with world.client() as client:
        _, module_id = await _course_with_module(world, client)
        await _save(world, client, module_id, publish=True)
        answers = [
            {"question_id": str(a.question_id), "option_id": str(a.option_id)}
            for a in await _right_answers(world, module_id)
        ]

    async def unpublish():
        async with world.client(raise_app_exceptions=False) as client:
            return await client.post(
                f"{BASE}/modules/{module_id}/assessment/unpublish", headers=world.h()
            )

    async def submit():
        async with world.client(raise_app_exceptions=False) as client:
            return await client.post(
                f"/v1/training/modules/{module_id}/assessment/attempts",
                json={"answers": answers},
                headers=world.h(world.employee),
            )

    unpublished, submitted = await asyncio.gather(unpublish(), submit())

    assert (unpublished.status_code, submitted.status_code) in {(200, 404), (409, 201)}, (
        unpublished.text,
        submitted.text,
    )
    status, attempts = await _state(world, module_id)
    assert (status, attempts) in {("draft", 0), ("published", 1)}


async def test_a_submission_paused_before_its_insert_neither_deadlocks_nor_loses_to_an_unpublish(
    world, monkeypatch
):
    """The window the other interleavings do not cover: the submission has read the assessment but
    has not yet inserted its attempt. With the unpublish arriving then, the two used to take their
    row locks in opposite orders (unpublish: module, then assessment; submission: assessment, then -
    through the attempt's foreign key - module), which Postgres can only resolve by aborting one of
    them with a deadlock error. Both transactions must instead finish cleanly: the submission wins,
    the unpublish is refused, and no attempt exists on a draft."""
    async with world.client() as client:
        _, module_id = await _course_with_module(world, client)
        await _save(world, client, module_id, publish=True)
    answers = await _right_answers(world, module_id)

    reached, release = asyncio.Event(), asyncio.Event()
    real_insert = assessments_repo.insert_attempt

    async def paused_insert(*args, **kwargs):
        reached.set()
        await release.wait()
        return await real_insert(*args, **kwargs)

    monkeypatch.setattr(assessments_repo, "insert_attempt", paused_insert)
    submitting = asyncio.create_task(
        _in_own_transaction(
            world,
            lambda s: assessments_service.submit_attempt(
                s,
                world.employee.user,
                module_id=UUID(module_id),
                answers=answers,
                idempotency_key=f"race-{uuid4()}",
            ),
        )
    )
    await asyncio.wait_for(reached.wait(), timeout=15)  # graded, about to insert
    unpublishing = asyncio.create_task(_unpublish(world, module_id))
    assert await _still_waiting(unpublishing), "the unpublish did not wait for the submission"
    release.set()

    attempt = await asyncio.wait_for(
        submitting, timeout=30
    )  # a deadlock would abort one of the two
    with pytest.raises(ConflictError, match="has attempts"):
        await asyncio.wait_for(unpublishing, timeout=30)
    assert attempt.passed is True
    assert await _state(world, module_id) == ("published", 1)
