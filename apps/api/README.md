# edutoon-api

FastAPI service for EduToon AI.

```bash
uv sync
uv run uvicorn edutoon.main:app --reload --port 8000
```

Requires the environment variables listed in the repository root `.env.example`.

## Clerk Local Setup

Two of those variables, `CLERK_ISSUER` and `CLERK_JWKS_URL`, come from a
Clerk application rather than the local Docker stack. The API fails to
start without them (`get_settings()` raises `MissingSettingsError` naming
both), so set them up before running `make dev`.

1. **Create a Clerk application.** Sign in at
   [dashboard.clerk.com](https://dashboard.clerk.com), click **Create
   application**, and give it any name (a free "Development" instance is
   enough for local work).
2. **Include `email` in the session token.** By default Clerk's session
   token only carries `sub`; this API's Clerk provider
   (`src/edutoon/providers/clerk.py`) also requires an `email` claim to
   provision a `users` row. In the dashboard, go to **Sessions > Customize
   session token** and add:
   ```json
   { "email": "{{user.primary_email_address}}" }
   ```
3. **Find `CLERK_ISSUER`.** In **Configure > API Keys**, copy the
   **Frontend API URL** (looks like `https://your-app-name-12.clerk.accounts.dev`,
   or `https://clerk.<your-domain>` on a production instance with a custom
   domain). That URL, unchanged, is `CLERK_ISSUER`.
4. **Find `CLERK_JWKS_URL`.** Append `/.well-known/jwks.json` to the same
   Frontend API URL.
5. **Set both in `apps/api/.env`:**
   ```bash
   CLERK_ISSUER=https://your-app-name-12.clerk.accounts.dev
   CLERK_JWKS_URL=https://your-app-name-12.clerk.accounts.dev/.well-known/jwks.json
   ```

Neither value is a secret key — they're derived from your instance's public
Frontend API URL, safe to share within the team (just not worth committing,
hence `.env` staying git-ignored). No Clerk account is needed to run the
test suite: `tests/test_auth.py` signs its own test JWTs against a local,
throwaway RSA keypair and never calls Clerk.
