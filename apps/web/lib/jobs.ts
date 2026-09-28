import type { Job, JobKind } from '@edutoon/contracts';
import { safeServerMessage } from '@/lib/api/errors';

export function isActiveJob(job: Job): boolean {
  return job.status === 'queued' || job.status === 'running';
}

/** The most recently created job of one kind. The API lists newest first, but do not rely on it. */
export function latestJobOfKind(jobs: readonly Job[] | undefined, kind: JobKind): Job | undefined {
  if (!jobs) return undefined;
  let latest: Job | undefined;
  for (const job of jobs) {
    if (job.kind !== kind) continue;
    if (!latest || Date.parse(job.created_at) > Date.parse(latest.created_at)) latest = job;
  }
  return latest;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export type ExtractionMode = 'pdf' | 'keywords' | 'pdf_keywords';

export interface ExtractionSummary {
  mode: ExtractionMode | null;
  topicCount: number | null;
  /** The model was shown only part of a long document. */
  truncated: boolean;
  chunksTotal: number | null;
  chunksUsed: number | null;
}

const asCount = (value: unknown): number | null =>
  typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;

/**
 * What an `extract_topics` job reported. Its `result` is untyped in the API's contract, so it is
 * read field by field and a missing or odd field is simply absent - never an exception.
 */
export function readExtractionSummary(job: Job | undefined): ExtractionSummary | null {
  if (!job || job.status !== 'succeeded' || !isRecord(job.result)) return null;
  const result = job.result;
  const mode =
    result.mode === 'pdf' || result.mode === 'keywords' || result.mode === 'pdf_keywords'
      ? result.mode
      : null;
  return {
    mode,
    topicCount: asCount(result.topic_count),
    truncated: result.truncated === true,
    chunksTotal: asCount(result.chunks_total),
    chunksUsed: asCount(result.chunks_used),
  };
}

const GENERIC_FAILURE = "This step didn't finish. You can try again.";

export interface ClaimExtractionFailure {
  topicId: string | null;
  topicTitle: string | null;
  /** Already screened for people (the API writes it through the same job-error path). */
  error: string;
}

export interface ClaimExtractionSummary {
  topicsEligible: number | null;
  topicsProcessed: number | null;
  topicsSkipped: number | null;
  topicsFailed: number | null;
  candidatesGenerated: number | null;
  claimsVerified: number | null;
  /** Verified counts by verdict, e.g. `{ supported: 3, unsupported: 1 }`. */
  claimsByStatus: Record<string, number>;
  failures: ClaimExtractionFailure[];
}

const asString = (value: unknown): string | null => (typeof value === 'string' ? value : null);

function readClaimsByStatus(value: unknown): Record<string, number> {
  if (!isRecord(value)) return {};
  const result: Record<string, number> = {};
  for (const [status, count] of Object.entries(value)) {
    const parsed = asCount(count);
    if (parsed !== null) result[status] = parsed;
  }
  return result;
}

function readFailures(value: unknown): ClaimExtractionFailure[] {
  if (!Array.isArray(value)) return [];
  const failures: ClaimExtractionFailure[] = [];
  for (const entry of value) {
    if (!isRecord(entry)) continue;
    const error = safeServerMessage(entry.error) ?? GENERIC_FAILURE;
    failures.push({
      topicId: asString(entry.topic_id),
      topicTitle: asString(entry.topic_title),
      error,
    });
  }
  return failures;
}

/**
 * What an `extract_claims` job reported. Its `result` is untyped in the API's contract, so it is
 * read field by field and a missing or odd field is simply absent - never an exception.
 */
export function readClaimExtractionSummary(job: Job | undefined): ClaimExtractionSummary | null {
  if (!job || job.status !== 'succeeded' || !isRecord(job.result)) return null;
  const result = job.result;
  return {
    topicsEligible: asCount(result.topics_eligible),
    topicsProcessed: asCount(result.topics_processed),
    topicsSkipped: asCount(result.topics_skipped_already_extracted),
    topicsFailed: asCount(result.topics_failed),
    candidatesGenerated: asCount(result.candidates_generated),
    claimsVerified: asCount(result.claims_verified),
    claimsByStatus: readClaimsByStatus(result.claims_by_status),
    failures: readFailures(result.failures),
  };
}

/**
 * Why a job failed, in words for its owner. The API writes `error` for people, but it is screened
 * again here so nothing technical can ever reach the screen.
 */
export function jobFailureMessage(job: Job | undefined): string {
  return safeServerMessage(job?.error) ?? GENERIC_FAILURE;
}

export type WaitLevel = 'normal' | 'slow' | 'stalled';

export interface WaitAssessment {
  level: WaitLevel;
  elapsedMs: number;
  /** Set when the wait is longer than usual, so the person is told rather than left guessing. */
  message: string | null;
}

// How long each phase may take before it is called out. Deliberately generous: a long PDF is a
// long model call.
const QUEUED_SLOW_MS = 90_000;
const RUNNING_SLOW_MS = 4 * 60_000;
const RUNNING_STALLED_MS = 12 * 60_000;

/** How long a queued or running job has been waiting, and whether that is unusual. */
export function assessWait(job: Job, now: number = Date.now()): WaitAssessment {
  const queued = job.status === 'queued';
  const since = Date.parse(queued ? job.created_at : (job.started_at ?? job.created_at));
  const elapsedMs = Math.max(0, now - since);

  if (queued && elapsedMs > QUEUED_SLOW_MS) {
    return {
      level: 'slow',
      elapsedMs,
      message:
        "This is still waiting for its turn. It's taking longer than usual, but nothing is lost - you can leave this page and come back.",
    };
  }
  if (!queued && elapsedMs > RUNNING_STALLED_MS) {
    return {
      level: 'stalled',
      elapsedMs,
      message:
        "This is taking much longer than expected. Refresh to check on it; if it still hasn't finished, it will be marked as failed and you can try again.",
    };
  }
  if (!queued && elapsedMs > RUNNING_SLOW_MS) {
    return {
      level: 'slow',
      elapsedMs,
      message: 'Longer documents take longer. You can leave this page - we will keep going.',
    };
  }
  return { level: 'normal', elapsedMs, message: null };
}
