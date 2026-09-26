/**
 * Turning a failed API call into something a person can act on.
 *
 * The API answers every failure with `{ "error": { "code", "message", "details"? } }` (its
 * OpenAPI document still declares FastAPI's default `{ "detail" }` shape for 422s, so the body is
 * read defensively rather than trusted). What the API says is only ever *shown* when it is a
 * short, plain-language sentence written for people; anything that looks technical - a stack
 * trace, SQL, a provider's response - is replaced by a generic sentence. Nothing here ever
 * shows a raw response.
 */

export type ApiErrorKind =
  | 'network'
  | 'timeout'
  | 'unauthorized'
  | 'not_found'
  | 'conflict'
  | 'too_large'
  | 'invalid'
  | 'server'
  | 'unknown';

export interface FieldError {
  /** Dotted location, e.g. `body.title`. */
  path: string;
  message: string;
}

export interface ApiErrorInit {
  kind: ApiErrorKind;
  status: number | null;
  code: string | null;
  serverMessage: string | null;
  fieldErrors: FieldError[];
  requestId?: string | null;
}

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly code: string | null;
  /** What the API said, already screened by {@link safeServerMessage}. */
  readonly serverMessage: string | null;
  readonly fieldErrors: FieldError[];
  readonly requestId: string | null;

  constructor(init: ApiErrorInit) {
    super(`API request failed (${init.status ?? init.kind})`);
    this.name = 'ApiError';
    this.kind = init.kind;
    this.status = init.status;
    this.code = init.code;
    this.serverMessage = init.serverMessage;
    this.fieldErrors = init.fieldErrors;
    this.requestId = init.requestId ?? null;
  }

  /**
   * Whether the request may or may not have taken effect: worth retrying with the *same*
   * idempotency key, because a replay is safe and a fresh key could do the work twice.
   */
  get outcomeUnknown(): boolean {
    return this.kind === 'network' || this.kind === 'timeout' || this.kind === 'server';
  }

  /** Whether trying the same request again could plausibly succeed. */
  get retryable(): boolean {
    return this.outcomeUnknown;
  }
}

function kindForStatus(status: number): ApiErrorKind {
  if (status === 401) return 'unauthorized';
  if (status === 404) return 'not_found';
  if (status === 409) return 'conflict';
  if (status === 413) return 'too_large';
  if (status === 400 || status === 422) return 'invalid';
  if (status >= 500) return 'server';
  return 'unknown';
}

// Things that only ever appear in text that was never meant for a person.
const TECHNICAL_PATTERNS = [
  /traceback/i,
  /\bstack\b/i,
  /\bsql\b/i,
  /sqlalchemy|asyncpg|psycopg|postgres/i,
  /\b(select|insert|update|delete)\b[^.]*\b(from|into|set)\b/i,
  /\.py\b|\.tsx?\b|\.js\b/i,
  /[A-Za-z]:\\|\/usr\/|\/var\/|\/home\//,
  /\bat 0x[0-9a-f]+/i,
  /exception|errno|constraint|violates/i,
  /api[_ -]?key|bearer\s|sk-[a-z0-9]/i,
  /anthropic|openai/i,
  /\b(status|error)\s*(code)?\s*[:=]\s*\d{3}\b/i,
  /[{}]/,
];

const MAX_SERVER_MESSAGE_CHARS = 280;

/** The API's own words, if they are short and plain enough to show; otherwise `null`. */
export function safeServerMessage(message: unknown): string | null {
  if (typeof message !== 'string') return null;
  const text = message.replace(/\s+/g, ' ').trim();
  if (text.length === 0 || text.length > MAX_SERVER_MESSAGE_CHARS) return null;
  if (TECHNICAL_PATTERNS.some((pattern) => pattern.test(text))) return null;
  return text;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function readFieldErrors(details: unknown): FieldError[] {
  if (!isRecord(details) || !Array.isArray(details.field_errors)) return [];
  const result: FieldError[] = [];
  for (const entry of details.field_errors) {
    if (isRecord(entry) && typeof entry.path === 'string' && typeof entry.message === 'string') {
      result.push({ path: entry.path, message: entry.message });
    }
  }
  return result;
}

/** Build an {@link ApiError} from a non-2xx response and whatever body it carried. */
export function apiErrorFromResponse(response: Response, body: unknown): ApiError {
  const envelope = isRecord(body) && isRecord(body.error) ? body.error : null;
  return new ApiError({
    kind: kindForStatus(response.status),
    status: response.status,
    code: envelope && typeof envelope.code === 'string' ? envelope.code : null,
    serverMessage: envelope ? safeServerMessage(envelope.message) : null,
    fieldErrors: envelope ? readFieldErrors(envelope.details) : [],
    requestId: response.headers.get('X-Request-Id'),
  });
}

/** Build an {@link ApiError} from a request that never produced a response. */
export function apiErrorFromFailure(cause: unknown): ApiError {
  const timedOut =
    cause instanceof DOMException && (cause.name === 'TimeoutError' || cause.name === 'AbortError');
  return new ApiError({
    kind: timedOut ? 'timeout' : 'network',
    status: null,
    code: null,
    serverMessage: null,
    fieldErrors: [],
  });
}

export interface ErrorCopy {
  title: string;
  message: string;
}

export interface DescribeOptions {
  /** What the person was trying to do, in the form "save your changes" (no trailing full stop). */
  action?: string;
}

/**
 * Words for an error, safe to put on screen. `action` shapes the fallback ("We couldn't save
 * your changes"), and the API's own sentence is used only where it is safe and helpful
 * (a conflict or a rejected input), never for a server fault.
 */
export function describeError(error: unknown, options: DescribeOptions = {}): ErrorCopy {
  const action = options.action ?? 'complete that';

  if (!(error instanceof ApiError)) {
    return {
      title: `We couldn't ${action}`,
      message: 'Something unexpected happened. Please try again.',
    };
  }

  switch (error.kind) {
    case 'network':
      return {
        title: "We couldn't reach EduToon",
        message: 'Check your internet connection and try again.',
      };
    case 'timeout':
      return {
        title: 'That is taking longer than expected',
        message: `We didn't get a reply in time. Nothing is lost - please try to ${action} again.`,
      };
    case 'unauthorized':
      return {
        title: 'Your session has ended',
        message: 'Please sign in again to continue.',
      };
    case 'not_found':
      return {
        title: "We couldn't find that",
        message: 'It may have been removed, or it may belong to a different account.',
      };
    case 'too_large':
      return {
        title: 'That file is too large',
        message: 'Choose a smaller PDF and try again.',
      };
    case 'conflict':
      return {
        title: `We couldn't ${action} right now`,
        message:
          error.serverMessage ??
          'This changed while you were working. Refresh the page and try again.',
      };
    case 'invalid':
      return {
        title: `We couldn't ${action}`,
        // A 422 carries validator wording written for developers; the forms check their own
        // input first, so all that is left to say here is that something was not accepted.
        message:
          error.status === 422
            ? 'Some of the details were not accepted. Please review them and try again.'
            : (error.serverMessage ?? 'Some of the details were not accepted. Please review them.'),
      };
    case 'server':
      return {
        title: 'Something went wrong on our side',
        message: `We couldn't ${action}. Please try again in a moment.`,
      };
    default:
      return {
        title: `We couldn't ${action}`,
        message: 'Something unexpected happened. Please try again.',
      };
  }
}
