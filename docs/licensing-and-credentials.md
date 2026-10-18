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

### Invitation email: BLOCKED on one setting (and one bug found and fixed 2026-10-18)

Update 2026-10-18: the key in `apps/web/.env.local` was checked read-only against Clerk (`GET /v1/jwks`: 200,
one signing key; nothing printed from the key). The same key got **HTTP 403** from the API's default
`Python-urllib` User-Agent, so every invitation would have failed with "Clerk refused the invitation (HTTP 403)"
even with the secret configured. `providers/clerk.py` now sends its own `User-Agent` (regression test:
`tests/test_clerk_invitations_provider.py`). The only thing left is the step below, which sends one real email
from the owner's Clerk instance and so waits for an explicit yes.


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
| RapidOCR 3.10.0 (the code) | Apache-2.0 | `METADATA` | Permissive |
| RapidOCR's bundled OCR models (`PP-OCRv6_det_small`, `PP-OCRv6_rec_small`, `ch_ppocr_mobile_v2.0_cls`) | Apache-2.0; copyright held by Baidu / the PaddleOCR rights holders, not by RapidOCR | `MODEL_LICENSES.md` shipped in the wheel (a notice that separates the code's copyright from the weights'). It says its coverage is limited to the models it lists | Apache-2.0 asks for the licence text and notices to travel with a redistributed copy. The notice file is in the wheel |
| ONNX Runtime 1.30.0 | MIT | `METADATA` | Permissive |
| pypdfium2 5.14.0 | BSD-3-Clause and Apache-2.0 (plus dependency licences; the wheel also carries a CC-BY-4.0 text) | `METADATA`, `LICENSES/` | Permissive; CC-BY-4.0 asks for attribution of whatever it covers (not traced further) |
| `qwen2.5:7b-instruct` (language model, 7.6B parameters, Q4_K_M, run through Ollama) | Apache-2.0 | `ollama show qwen2.5:7b-instruct --license` prints the Apache-2.0 text packaged with the model | A *model's* licence is separate from Ollama's. This is the licence text the Ollama package carries; it was **not** cross-checked against the upstream Hugging Face model card. Other Qwen2.5 sizes are licensed differently, so do not assume the same for them |
| `bge-m3` (embeddings, 566.7M parameters, F16, through Ollama) | MIT | `ollama show bge-m3 --license` prints the MIT text packaged with the model (its copyright line is the unfilled template "[year] [fullname]") | As above: from the package, not cross-checked upstream. Training-data terms are not covered by that notice and were not reviewed |
| Ollama (the runtime that serves both models) | not checked in this pass (its repository states MIT) | | It is a separate program the user installs; nothing of it is bundled in this repository |
| Noto Sans and Noto Sans Mono (variable fonts, bundled and used by the renderer) | SIL Open Font License 1.1 | `apps/api/src/edutoon/providers/renderer/assets/fonts/OFL.txt` is in the repository | The only third-party assets that are *redistributed in this repository*. The OFL permits bundling and use; it forbids selling the font by itself. Keep `OFL.txt` with them |
| Pillow 12.3.0 | MIT-CMU (HPND-style) | `METADATA` | Permissive |
| fpdf2 2.8.8 | LGPL-3.0-only | `METADATA` | Used only by `scripts/make_qa_inputs.py` to make sample PDFs, not by the running product. LGPL obligations apply to a distributed build that links it |
| python-pptx 1.0.2 | MIT | `METADATA` | Permissive |

## Using something locally is not the same as distributing it

Everything above is evidence about *licences*, not legal advice. For the academic submission the
position is: the project runs on one machine, the models, Piper, FFmpeg and Ollama are separate programs
or downloads the user installs, and the repository itself redistributes only its own source and the two
Noto fonts (with `OFL.txt`). Running GPL software (Piper, this FFmpeg build) on your own machine for your
own use creates no distribution obligation by itself. Shipping a product that *contains* them, selling
a hosted service built on them, or handing customers an installer, does raise the questions in the
table (GPL for Piper and FFmpeg-with-x264, H.264/AAC patent licensing, the voice's own terms), and calling
a GPL program as a separate executable does not automatically remove every obligation. **Commercial use
or distribution needs a licence review by whoever owns that decision.** Nothing here settles it.

## What is NOT checked

- The licences of every transitive Python and npm dependency (a licence scan was not run).
- The licence text of `qwen2.5:7b-instruct` and `bge-m3` against their upstream Hugging Face model cards
  (only the text packaged by Ollama was read), and the terms of their training data.
- Ollama's own licence (not read on this machine), and the CC-BY-4.0 text inside the pypdfium2 wheel.
- Anything about the legal status of generated educational videos in your jurisdiction.

## The narration voice is British, not Indian

`en_GB-cori-medium` is a British English female voice. A project whose language is `en-IN` is **written**
in Indian English and **narrated** in this British voice; the narration page says so. No Indian-English
Piper voice has been chosen or listened to.
