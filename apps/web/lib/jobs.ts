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

export interface ScriptGenerationProgress {
  stage: string;
  topicIndex: number | null;
  topicCount: number | null;
  topicTitle: string | null;
}

/**
 * What a `generate_script` job's interim `payload.progress` reports while it is still running
 * (Phase 7 - see `services.scripts` on the API side). Untyped in the API's contract, so it is
 * read field by field, exactly like `readExtractionSummary` / `readClaimExtractionSummary`: a
 * missing or odd field is simply absent, never an exception.
 */
export function readScriptGenerationProgress(
  job: Job | undefined,
): ScriptGenerationProgress | null {
  if (!job || job.status !== 'running' || !isRecord(job.payload)) return null;
  const progress = job.payload.progress;
  if (!isRecord(progress) || typeof progress.stage !== 'string') return null;
  return {
    stage: progress.stage,
    topicIndex: asCount(progress.topic_index),
    topicCount: asCount(progress.topic_count),
    topicTitle: asString(progress.topic_title),
  };
}

const SCRIPT_GENERATION_STAGE_LABELS: Record<string, string> = {
  preparing_outline: 'Planning the lesson outline',
  generating_topic: 'Writing the script',
  correcting_duration: 'Adjusting the length',
  validating_provenance: 'Checking every source',
  finalising: 'Finishing up',
};

/** A one-line label for a script generation's current stage, naming the topic when known. */
export function scriptGenerationStageLabel(progress: ScriptGenerationProgress): string {
  if (
    progress.stage === 'generating_topic' &&
    progress.topicIndex !== null &&
    progress.topicCount !== null
  ) {
    const of = `topic ${progress.topicIndex + 1} of ${progress.topicCount}`;
    return progress.topicTitle ? `Writing ${of}: ${progress.topicTitle}` : `Writing ${of}`;
  }
  return SCRIPT_GENERATION_STAGE_LABELS[progress.stage] ?? 'Writing your script';
}

export interface SynthesisProgress {
  stage: string;
  segmentIndex: number | null;
  segmentCount: number | null;
}

/**
 * What a `synthesise_voice` job's interim `payload.progress` reports while it is still running
 * (Phase 8 - see `services.script_audio` on the API side). Same untyped, field-by-field reading
 * as `readScriptGenerationProgress`.
 */
export function readSynthesisProgress(job: Job | undefined): SynthesisProgress | null {
  if (!job || job.status !== 'running' || !isRecord(job.payload)) return null;
  const progress = job.payload.progress;
  if (!isRecord(progress) || typeof progress.stage !== 'string') return null;
  return {
    stage: progress.stage,
    segmentIndex: asCount(progress.segment_index),
    segmentCount: asCount(progress.segment_count),
  };
}

const SYNTHESIS_STAGE_LABELS: Record<string, string> = {
  preparing: 'Preparing narration',
  synthesising_segment: 'Narrating your script',
  finalising: 'Finishing up',
};

/** A one-line label for a synthesis job's current stage, naming the section when known. */
export function synthesisStageLabel(progress: SynthesisProgress): string {
  if (
    progress.stage === 'synthesising_segment' &&
    progress.segmentIndex !== null &&
    progress.segmentCount !== null
  ) {
    return `Narrating section ${progress.segmentIndex + 1} of ${progress.segmentCount}`;
  }
  return SYNTHESIS_STAGE_LABELS[progress.stage] ?? 'Narrating your script';
}

export interface SynthesisResultSummary {
  segmentCount: number | null;
  readySegmentCount: number | null;
  failedSegmentIds: string[];
}

/**
 * What a finished `synthesise_voice` job reported - how many segments actually ended up with
 * valid audio, and which ones did not, so a partial run is never mistaken for a clean one.
 */
export function readSynthesisResult(job: Job | undefined): SynthesisResultSummary | null {
  if (!job || (job.status !== 'succeeded' && job.status !== 'failed') || !isRecord(job.result)) {
    return null;
  }
  const result = job.result;
  const failedSegmentIds = Array.isArray(result.failed_segment_ids)
    ? result.failed_segment_ids.filter((value): value is string => typeof value === 'string')
    : [];
  return {
    segmentCount: asCount(result.segment_count),
    readySegmentCount: asCount(result.ready_segment_count),
    failedSegmentIds,
  };
}

export interface StoryboardProgress {
  stage: string;
  segmentIndex: number | null;
  segmentCount: number | null;
}

/**
 * What a `generate_storyboard` job's interim `payload.progress` reports while it is still
 * running (Phase 9 - see `services.storyboard` on the API side). Same untyped, field-by-field
 * reading as `readSynthesisProgress`.
 */
export function readStoryboardProgress(job: Job | undefined): StoryboardProgress | null {
  if (!job || job.status !== 'running' || !isRecord(job.payload)) return null;
  const progress = job.payload.progress;
  if (!isRecord(progress) || typeof progress.stage !== 'string') return null;
  return {
    stage: progress.stage,
    segmentIndex: asCount(progress.segment_index),
    segmentCount: asCount(progress.segment_count),
  };
}

const STORYBOARD_STAGE_LABELS: Record<string, string> = {
  preparing: 'Preparing storyboard',
  processing_segment: 'Storyboarding your script',
  finalising: 'Finishing up',
};

/** A one-line label for a storyboard job's current stage, naming the section when known. */
export function storyboardStageLabel(progress: StoryboardProgress): string {
  if (
    progress.stage === 'processing_segment' &&
    progress.segmentIndex !== null &&
    progress.segmentCount !== null
  ) {
    return `Storyboarding section ${progress.segmentIndex + 1} of ${progress.segmentCount}`;
  }
  return STORYBOARD_STAGE_LABELS[progress.stage] ?? 'Storyboarding your script';
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

export interface RemovedSentence {
  sentence: string;
  reason: string;
  topic: string | null;
  /** The rest of that passage was too short to read out, so the whole passage went. */
  segmentDropped: boolean;
}

export interface SupportCheck {
  /** Whether the AI judge ran (the rule-based removals are recorded either way). */
  enabled: boolean;
  judgedSentences: number;
  uncheckedSentences: number;
  removed: RemovedSentence[];
  /** Topics whose writing could not be supported, so their verified claims are read out as they stand. */
  fallbackTopics: string[];
}

export interface ScriptChecks {
  /** The lesson rests on the AI's general knowledge: it was built from a topic alone. */
  generalKnowledge: boolean;
  /** The sentence-level support check, or `null` when it did not run for this script. */
  support: SupportCheck | null;
}

/**
 * What the server's own checks did to a script: whether it is a general-knowledge lesson, and the
 * sentence-level support check (how many sentences were judged, which were removed and why).
 * `null` when no `generate_script` job result is known for the script (an older script), so "no
 * check ran" is never shown as "nothing was found".
 */
export function readScriptChecks(
  jobs: readonly Job[] | undefined,
  scriptId: string,
): ScriptChecks | null {
  const job = jobs?.find(
    (candidate) =>
      candidate.kind === 'generate_script' &&
      candidate.status === 'succeeded' &&
      isRecord(candidate.result) &&
      candidate.result.script_id === scriptId,
  );
  if (!job || !isRecord(job.result)) return null;
  const generalKnowledge = job.result.general_knowledge === true;
  const raw = job.result.support_check;
  let support: SupportCheck | null = null;
  if (isRecord(raw)) {
    const removed: RemovedSentence[] = [];
    if (Array.isArray(raw.removed_sentences)) {
      for (const entry of raw.removed_sentences) {
        if (!isRecord(entry) || typeof entry.sentence !== 'string') continue;
        removed.push({
          sentence: entry.sentence,
          reason: typeof entry.reason === 'string' ? entry.reason : '',
          topic: typeof entry.topic === 'string' ? entry.topic : null,
          segmentDropped: entry.segment_dropped === true,
        });
      }
    }
    support = {
      enabled: raw.enabled !== false,
      judgedSentences: asCount(raw.judged_sentences) ?? 0,
      uncheckedSentences: asCount(raw.unchecked_sentences) ?? 0,
      removed,
      fallbackTopics: Array.isArray(raw.fallback_sections)
        ? // The same topic can fall back in the first pass and again in the length correction.
          [
            ...new Set(
              raw.fallback_sections.flatMap((entry) =>
                isRecord(entry) && typeof entry.topic === 'string' ? [entry.topic] : [],
              ),
            ),
          ]
        : [],
    };
  }
  return { generalKnowledge, support };
}

export interface WeakCitation {
  /** Zero-based position of the passage in the script (`ScriptSegment.segment_index`). */
  segmentIndex: number;
  /** Share (0 to 1) of the passage's meaningful words that appear in the claims it cites. */
  coverage: number;
}

/**
 * The passages a `generate_script` job flagged as having weak citations, for the script it wrote.
 * `null` when no such job is known (an older script, or a job that predates the flag), so "none
 * flagged" and "not checked" are never confused. The job's `result` is untyped in the API's
 * contract, so it is read field by field and an odd entry is skipped, never an exception.
 */
export function readWeakCitations(
  jobs: readonly Job[] | undefined,
  scriptId: string,
): WeakCitation[] | null {
  const job = jobs?.find(
    (candidate) =>
      candidate.kind === 'generate_script' &&
      candidate.status === 'succeeded' &&
      isRecord(candidate.result) &&
      candidate.result.script_id === scriptId,
  );
  if (!job || !isRecord(job.result) || !Array.isArray(job.result.weak_citation_segments)) {
    return null;
  }
  const weak: WeakCitation[] = [];
  for (const entry of job.result.weak_citation_segments) {
    if (!isRecord(entry)) continue;
    const segmentIndex = asCount(entry.segment_index);
    const coverage = entry.coverage;
    if (segmentIndex === null || typeof coverage !== 'number' || !Number.isFinite(coverage)) {
      continue;
    }
    weak.push({ segmentIndex, coverage });
  }
  return weak;
}
