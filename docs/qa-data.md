# QA data in the development database (inventory, 2026-10-18)

Nothing was deleted in this pass. This is what exists, whose it is, and how to remove the disposable
part safely when you want to. Everything here is in the **development** database (`edutoon`), never a
shared one.

## Whose is what

| Owner | Projects | What they are |
| --- | ---: | --- |
| `ui-qa@example.test` (the development stand-in administrator) | 16 | Every project made by a QA session through the stand-in token: titles contain "QA", "acceptance" or "exp". **Disposable.** |
| `durgesh200417@gmail.com` (you) | 9 | Earlier work and verification runs: `Machine Learning` (failed), `Chapter 1` (failed), `chapter 1` (the finance video, 2 videos), `Demo: Workplace safety (seeded)` (2 videos, used by the seeded demo course) and five "(verification)" / "UI paste run" projects. **Possibly useful; ambiguous: leave them.** |

Users: `ui-qa@example.test` (admin), `ui-qa-employee@example.test` (employee), your admin account and
`durgesh200417+employee@gmail.com` (employee).

## Courses

| Course | Made by | Notes |
| --- | --- | --- |
| `Demo: Safety induction` | the seed script | Two modules from the seeded demo project. Keep for demos |
| `QA course: generated lessons` | QA | Contains a module titled `Industrial ReThe Industrial Revolutionvolution (QA: pasted text)` |
| `QA course: locking check` | QA | The two-module locking check; the employee has passed module 1 |
| `Acceptance course: heart and Python` | QA | Built in the UI on the new acceptance videos; the employee has passed module 1 |

**The garbled module title** was a one-off data-entry slip: the title field is prefilled with the project's
name and the text was typed into the middle of it. The field and the request are correct (see
`training.test.tsx`, "Naming a module"). A module cannot be renamed in the product, and one that a learner
has attempted cannot be deleted (`409`, by design), so it can only be unpublished, or its course archived.

## Failed jobs

21 failed jobs remain from earlier sessions and from this one (for example the script runs that failed
after 6 and 17 minutes before the bugs behind them were fixed). They are kept on purpose: they are the
record of what failed and why.

## How to remove the disposable part, if and when you want to

Reversible only as far as a dump is: take one first.

```bash
docker exec edutoon-postgres-1 pg_dump -U edutoon -Fc edutoon > edutoon_before_cleanup.dump
```

Then, as the QA administrator, through the API or the UI (the product enforces its own rules, so
referential integrity holds):

1. Archive the three QA courses (`POST /v1/admin/training/courses/{id}/archive`). A course with learner
   attempts cannot be deleted, only archived.
2. Delete each QA project (`DELETE /v1/projects/{id}`): this also removes its uploaded files from object
   storage after the deletion commits. A project whose video is a training module's source is safe to
   delete: the module holds its own copy.
3. Do not delete anything owned by your own account without looking at it first.

Deleting rows with SQL is not recommended: the ledger-style tables are append-only and several
foreign keys are `RESTRICT` on purpose.

## Addendum, 2026-10-18 (verification pass)

Nothing was deleted from the owner's account. The pass added 12 projects under the QA account, all titled
`(v2 ...)`: three offered-mode runs (electric circuits, bridges, ecosystems with keywords), four runs of modes that
are now outside the offered scope (beginner and advanced instructions, a scanned PDF, topic only), two scan-failure
probes, and three claim-limit comparison projects (4, 8, 10). Their scene sets were cleared and rebuilt twice through
the product's own whole-set replace, so their storyboards reflect the final rules. One course, `v2 verification
course: ecosystems and Python`, was added by the learning-platform check. Two empty probe projects made to confirm
the scope refusals were deleted.
