# Licensing and credentials: what was checked, and what is still your decision

Checked on 2026-10-18 from the files on this machine and, for the voice, from the model card at its
source. Nothing here is legal advice. "Works" and "may be distributed" are different questions, and this
file only answers the first with evidence.

## Credentials

| Item | Where | What was found | Action |
| --- | --- | --- | --- |
| Clerk test secret key | `apps/web/.env.local` only | A Clerk *test-mode* key pair (`pk_test_`, `sk_test_`), same instance as the API's issuer (`scripts/check_clerk.py` passes) | Rotation is **optional and precautionary**. There is no evidence in the repository that it left this machine. If you want to rotate: Clerk dashboard, API keys, roll the secret key, paste the new value into `apps/web/.env.local`, restart the web app. Never paste it into chat. |
| Provider API keys | `apps/api/.env` | `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are 23 and 20 characters: too short for real keys, i.e. placeholders. The local AI backend never uses them | None |
| Database and object-store credentials | `apps/api/.env` | Local Docker defaults for the development containers only | None for local use. Never reuse them anywhere reachable |
| The copy of `apps/api/.env` once left in `C:\ew` | deleted 2026-10-17 with your OK | It held only the items above (placeholders and local-development defaults), not the Clerk secret | None beyond the above |
| `CLERK_SECRET_KEY` for the **API** | not set in `apps/api/.env` | `providers.clerk.invitations_configured()` is `False` | See "Invitation email" |

### Invitation email: BLOCKED on one setting

Inviting someone calls Clerk's `POST /v1/invitations` (`providers/clerk.send_invitation`), and Clerk sends
the email. It cannot work until the API process has `CLERK_SECRET_KEY`:

1. Put the same Clerk test secret key that is in `apps/web/.env.local` into `apps/api/.env` as
   `CLERK_SECRET_KEY=...`. (I did not copy a secret between files for you.)
2. Restart the API.
3. As an administrator, open a course, "Invite someone new", enter an address you control.
4. Check the inbox. Clerk's test instance may restrict who it will email; if nothing arrives, look at the
   Clerk dashboard under Invitations.

Until step 4 happens, "invitation email delivery" is **unverified**: the request shape follows Clerk's
documentation and is exercised in tests with the network call replaced.

### Real sign-in is unverified

`check_clerk.py` confirms the configuration, that the JWKS URL is reachable and lists a signing key, and
that both apps use the same Clerk instance. Signing in as a person, which needs a password or a social
login, was not done by an automated session and cannot be. Everything above the sign-in screen (roles,
route policies, the `403 not_invited` rule, the learner and administrator screens) was exercised with the
development stand-in tokens.

## Licences of what the video pipeline uses

| Component | Licence | Evidence | What it means for distribution (not decided here) |
| --- | --- | --- | --- |
| Piper (`piper-tts` 1.8.0, the speech engine) | GPL-3.0-or-later | `METADATA` of the installed package | A product that ships or links Piper has GPL obligations. Running it on your own server for your own staff is different from selling a download. **Needs a decision by whoever owns distribution.** |
| Voice `en_GB-cori-medium` | The model card states the *dataset* is public domain (LibriVox recordings) | `https://huggingface.co/rhasspy/piper-voices` model card for cori (medium) | The card says nothing separate about the trained model's own licence. Check the repository's top-level licence statement before commercial use |
| FFmpeg 9.0.2 (the Gyan "full" Windows build on this machine) | GPL-3.0 (it was built with `--enable-gpl --enable-version3`) and it includes `libx264` and `libx265` | `ffmpeg -version` | The GPL applies to a distributed build. Using it as an external program on your own machine is the common arrangement. A different build (LGPL, no x264) changes the answer |
| H.264 and AAC | Patent-licensed formats | not a code question | Patent pools may apply to commercial distribution of H.264 or AAC video. **Not settled**, and not settled by choosing an encoder |
| RapidOCR 3.10.0 | Apache-2.0 | `METADATA` | Permissive |
| ONNX Runtime 1.30.0 | MIT | `METADATA` | Permissive |
| pypdfium2 5.14.0 | BSD-3-Clause and Apache-2.0 (plus dependency licences) | `METADATA` | Permissive |
| Qwen2.5 7B Instruct and BGE-M3 (the local models) | not checked in this pass | | Read each model's licence on its Ollama / Hugging Face page before commercial use |

## What is NOT checked

- The licences of every transitive Python and npm dependency (a licence scan was not run).
- Whether the OCR models bundled in the RapidOCR wheel carry their own terms.
- Anything about the legal status of generated educational videos in your jurisdiction.

## The narration voice is British, not Indian

`en_GB-cori-medium` is a British English female voice. A project whose language is `en-IN` is **written**
in Indian English and **narrated** in this British voice; the narration page says so. No Indian-English
Piper voice has been chosen or listened to.
