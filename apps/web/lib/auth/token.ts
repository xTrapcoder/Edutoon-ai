import { getToken } from '@clerk/nextjs';

/**
 * The signed-in user's session token, sent to the API as `Authorization: Bearer <token>`.
 *
 * The API verifies it against the Clerk instance named by `CLERK_ISSUER` / `CLERK_JWKS_URL`, and
 * needs an `email` claim on it (see "Clerk Local Setup" in `apps/api/README.md`). This is the one
 * place the front end reads a credential; nothing else touches Clerk's tokens.
 */
export async function getApiToken(): Promise<string | null> {
  try {
    return await getToken();
  } catch {
    // Clerk not loaded yet, or the browser is offline: send no credentials, and the API's
    // 401 is turned into "Your session has ended" by the error layer.
    return null;
  }
}
