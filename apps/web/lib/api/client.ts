import createClient from 'openapi-fetch';
import type { paths } from '@edutoon/contracts';
import { getApiToken } from '@/lib/auth/token';
import { ApiError, apiErrorFromFailure, apiErrorFromResponse } from './errors';

/** Where the API lives. Set `NEXT_PUBLIC_API_BASE_URL` (see `apps/web/.env.local.example`). */
export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8000'
).replace(/\/+$/, '');

/** How long an ordinary request may take before the person is told, rather than left waiting. */
export const REQUEST_TIMEOUT_MS = 20_000;
/** A PDF is parsed inside the upload request, so it is allowed longer. */
export const UPLOAD_TIMEOUT_MS = 180_000;

let client: ReturnType<typeof createClient<paths>> | undefined;

/**
 * The one typed API client. Its request and response types come from the generated
 * `@edutoon/contracts`, so a route or field the API does not have is a compile error here.
 * Created on first use so tests can install a fetch mock before it captures `fetch`.
 */
export function getApiClient() {
  if (!client) {
    client = createClient<paths>({ baseUrl: API_BASE_URL });
    client.use({
      async onRequest({ request }) {
        const token = await getApiToken();
        if (token) request.headers.set('Authorization', `Bearer ${token}`);
        return request;
      },
    });
  }
  return client;
}

/** A fresh idempotency key. The API dedupes a mutation on (caller, method, path, key). */
export function newIdempotencyKey(): string {
  return globalThis.crypto.randomUUID();
}

interface Settled<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/**
 * Await an `openapi-fetch` call and return its data, or throw an {@link ApiError}.
 *
 * `openapi-fetch` reports HTTP failures as values and network failures as rejections; this folds
 * both into one exception so TanStack Query, and every caller, deals with a single error type.
 */
export async function unwrap<T>(pending: Promise<Settled<T>>): Promise<T> {
  let settled: Settled<T>;
  try {
    settled = await pending;
  } catch (cause) {
    if (cause instanceof ApiError) throw cause;
    throw apiErrorFromFailure(cause);
  }
  if (settled.response.ok) {
    return settled.data as T;
  }
  throw apiErrorFromResponse(settled.response, settled.error);
}

export function timeoutSignal(ms: number = REQUEST_TIMEOUT_MS): AbortSignal {
  return AbortSignal.timeout(ms);
}
