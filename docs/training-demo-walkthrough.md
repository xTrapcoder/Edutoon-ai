# Training demo — manual HTTP walkthrough

A two-module course on the development stack, seeded by `apps/api/scripts/seed_training_demo.py`.
The administrator side was seeded through the real API; **the employee side is yours to walk
through** — nothing below has been run as the employee.

Shell: Git Bash (`curl`, `python`, `uv` on PATH). Real Clerk sign-in, real token verification; no
stand-in.

## What was seeded

| Thing | Value |
|---|---|
| Course | `Demo: Safety induction` — `01a13ec3-5762-75ae-87c2-c89762e078a2` (published) |
| Module 1 | `Welcome and the rules of the site` — `01a13ec3-651f-7960-8c5b-12fcfe272a8e` (640x360, 8 s, has a published assessment) |
| Module 2 | `Emergency exits and lifting` — `01a13ec3-72d1-7b1a-8b45-620af49a5af8` (1280x720, 8 s, no assessment) |
| Assessment (module 1) | 3 questions, pass threshold 70 % — so **3 of 3** is needed (2 of 3 is 66 %) |
| Employee | `durgesh200417+employee@gmail.com` — a **pending invitation**; no user, no enrolment yet |
| Administrator | `durgesh200417@gmail.com` (existing) |

The two videos are real, tiny MP4s (H.264 + AAC) made with FFmpeg; the project, approved script and
two render rows behind them were written directly (a full pipeline run needs a provider key). The
invitation e-mail is **not** sent by the API (`CLERK_SECRET_KEY` is not in `apps/api/.env`; the
invitation's delivery is `not_configured`), so the employee signs up themselves in step 3.

## 0. Start things

```bash
docker compose up -d --wait                                   # Postgres, Redis, MinIO (already up)
cd apps/api && uv run uvicorn edutoon.main:app --port 8000    # the API — leave it running
```

For step 3 only, in another terminal, the web app (sign-up page):

```bash
source /c/Users/durge/AppData/Local/Temp/n22/env.sh && pnpm --filter @edutoon/web dev
```

## 1. Helpers (paste once per shell)

Tokens live about a minute, so each call mints a fresh one. The helper prints only the token and it
is never shown.

```bash
API=http://localhost:8000
COURSE=01a13ec3-5762-75ae-87c2-c89762e078a2
M1=01a13ec3-651f-7960-8c5b-12fcfe272a8e
M2=01a13ec3-72d1-7b1a-8b45-620af49a5af8
ADMIN_EMAIL=durgesh200417@gmail.com
EMP_EMAIL=durgesh200417+employee@gmail.com
TOKEN_SCRIPT=/c/Projects/Edutoon-ai-master/Edutoon-ai-master/apps/api

tok() { (cd "$TOKEN_SCRIPT" && uv run python scripts/dev_clerk_token.py --email "$1" 2>/dev/null); }
key() { python -c "import uuid; print(uuid.uuid4())"; }
adm() { curl -sS -H "Authorization: Bearer $(tok $ADMIN_EMAIL)" -H "Idempotency-Key: $(key)" "$@"; }
emp() { curl -sS -H "Authorization: Bearer $(tok $EMP_EMAIL)"   -H "Idempotency-Key: $(key)" "$@"; }
pj()  { python -m json.tool; }
```

## 2. Administrator: look at what is there

```bash
adm $API/v1/admin/training/courses/$COURSE | pj            # status "published", 2 modules
adm $API/v1/admin/training/modules/$M1/assessment | pj     # status "published", is_correct visible to admins
adm $API/v1/admin/invitations | pj                         # one pending employee invitation
```

Note the option ids of the **correct** options (`is_correct: true`); you will need them in step 10.
They are, by label: Q1 *At the assembly point*, Q2 *Your supervisor*, Q3 *With a straight back and
bent knees*.

## 3. Employee: sign up (browser)

1. Open `http://localhost:3000/sign-up` and sign up with `durgesh200417+employee@gmail.com`.
2. Clerk e-mails a verification code to your inbox; enter it. (That verification is what puts
   `email_verified: true` in the token, which the API requires to provision someone.)
3. If Clerk refuses the sign-up (an instance restricted to invitations), create the user in the Clerk
   Dashboard → Users with that address instead, with the e-mail marked verified.

## 4. Employee: first request provisions the account

```bash
emp $API/v1/auth/me | pj           # role "employee" — created from the pending invitation
emp $API/v1/training/courses | pj  # {"items": []} — not enrolled yet
```

Expected on any other address: `403 not_invited` and no user row.

## 5. Administrator: find the employee and enrol them

```bash
adm $API/v1/admin/users | pj       # copy the employee's "id"
EMP_ID=<paste it>
adm -X POST $API/v1/admin/training/courses/$COURSE/enrolments \
    -H "Content-Type: application/json" -d "{\"user_id\": \"$EMP_ID\"}" | pj     # 201
```

## 6. Employee: the course, with module 2 locked

```bash
emp $API/v1/training/courses | pj
```

Expect two modules; module 2 has `"locked": true` and a `lock` naming module 1.

## 7. Employee: module 2's video is refused, module 1's plays

```bash
emp -i -o /dev/null -w "%{http_code}\n" $API/v1/training/modules/$M2/video       # 403
emp $API/v1/training/modules/$M2/video | pj                                      # code "module_locked", names module 1
emp -o /tmp/m1.mp4 -H "Range: bytes=0-" -w "%{http_code} %{size_download}\n" \
    $API/v1/training/modules/$M1/video                                           # 206 and the file's bytes
ffprobe -v error -show_entries stream=codec_name,width,height /tmp/m1.mp4        # h264 640x360 + aac
```

## 8. Employee: read the assessment — no answers in it

```bash
emp $API/v1/training/modules/$M1/assessment | pj
```

Questions with options (`id`, `position`, `label`) only. There is no `is_correct` anywhere.
Copy the question and option ids.

## 9. Employee: a failing attempt (1 of 3)

Answer Q1 correctly and Q2, Q3 wrongly (any other option):

```bash
emp -X POST $API/v1/training/modules/$M1/assessment/attempts \
    -H "Content-Type: application/json" \
    -d '{"answers": [
          {"question_id": "<Q1>", "option_id": "<Q1 assembly point>"},
          {"question_id": "<Q2>", "option_id": "<Q2 a wrong option>"},
          {"question_id": "<Q3>", "option_id": "<Q3 a wrong option>"}]}' | pj
```

Expect `201`, `correct_count: 1`, `question_count: 3`, `threshold_percent: 70`, `passed: false`,
`attempt_number: 1`. It tells you the score, **not which answers were right**. Module 2 is still
locked (repeat step 7's first command: 403).

## 10. Employee: a passing attempt (3 of 3)

Same request with the three correct option ids from step 2. Expect `201`, `correct_count: 3`,
`passed: true`, `attempt_number: 2`.

```bash
emp $API/v1/training/modules/$M1/assessment/attempts | pj     # both attempts, outcome only
emp -o /tmp/m2.mp4 -H "Range: bytes=0-" -w "%{http_code}\n" $API/v1/training/modules/$M2/video   # now 206
emp $API/v1/training/courses | pj                              # no module locked any more
```

## 11. Administrator: an attempted assessment is frozen

```bash
adm -X POST $API/v1/admin/training/modules/$M1/assessment/unpublish | pj       # 409 "has attempts"
adm -X PUT  $API/v1/admin/training/modules/$M1/assessment \
    -H "Content-Type: application/json" \
    -d '{"questions":[{"prompt":"x","options":[{"label":"a","is_correct":true},{"label":"b"}]}]}' | pj   # 409 "published"
adm -X DELETE $API/v1/admin/training/modules/$M1/assessment -o /dev/null -w "%{http_code}\n"            # 409
```

## 12. Boundaries worth one try each

```bash
emp $API/v1/admin/training/courses | pj                         # 403 — employees have no admin area
emp $API/v1/admin/training/modules/$M1 | pj                     # 404 — the same 404 as for a module that does not exist
emp $API/v1/training/modules/01a13ec3-0000-7000-8000-000000000000/assessment | pj   # 404 "Assessment not found."
```

## Cleaning up

An attempted course cannot be deleted (the history is kept): archive it with
`adm -X POST $API/v1/admin/training/courses/$COURSE/archive`. The seeded project, script and render rows
are labelled "Demo" (`Demo: Workplace safety (seeded)`). Remove the Clerk user for the employee in the
Clerk Dashboard if you no longer want it.
