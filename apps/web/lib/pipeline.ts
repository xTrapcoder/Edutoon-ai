import type {
  ClaimVerification,
  Job,
  Project,
  RenderProgress,
  RenderStatus,
  Script,
  Source,
  StoryboardStatus,
  SynthesisStatus,
  Topic,
} from '@edutoon/contracts';
import { eligibleAcceptedTopics } from '@/components/topics/topic-tree';
import { summariseClaimStatuses } from '@/lib/claims';
import { formatDuration, pluralise } from '@/lib/format';
import { languageLabel, orientationLabel } from '@/lib/generation-settings';
import { isActiveJob, latestJobOfKind } from '@/lib/jobs';
import { jobIsFor } from '@/lib/render';
import { isFileProject, sourceWords } from '@/lib/source-kind';
import type { Tone, Workflow } from '@/lib/workflow';

/**
 * The eight stages a project passes through, from source to finished video, each with the state
 * the API actually reports for it.
 *
 * Nothing here is remembered or assumed: every status is worked out from the project's sources,
 * topics, claims, jobs, script, narration, storyboard and render status as the API returns them.
 * A stage whose prerequisite is not met is `blocked`, never `completed`; a stage whose data has
 * not arrived (or could not be read) says so instead of guessing.
 */

export type PipelineStageId =
  'source' | 'settings' | 'topics' | 'claims' | 'script' | 'narration' | 'storyboard' | 'video';

export type PipelineStatus =
  'completed' | 'processing' | 'pending' | 'failed' | 'blocked' | 'checking' | 'unknown';

export const PIPELINE_STATUS: Record<PipelineStatus, { label: string; long: string; tone: Tone }> =
  {
    completed: { label: 'Completed', long: 'Completed', tone: 'success' },
    processing: { label: 'Processing', long: 'Processing', tone: 'info' },
    pending: { label: 'Pending', long: 'Pending', tone: 'neutral' },
    failed: { label: 'Failed', long: 'Failed', tone: 'danger' },
    blocked: { label: 'Blocked', long: 'Blocked — prerequisite required', tone: 'warning' },
    checking: { label: 'Checking…', long: 'Checking…', tone: 'neutral' },
    unknown: { label: 'Unavailable', long: 'Status unavailable', tone: 'warning' },
  };

export interface PipelineStage {
  id: PipelineStageId;
  label: string;
  status: PipelineStatus;
  /** What is really known, in a few words: "12 topics", "3 of 8 sections narrated". */
  detail: string | null;
  /** Why the stage cannot go ahead yet, when `status` is `blocked`. */
  blocker: string | null;
  /** The page that shows the stage. It always opens: a blocked stage says what it is waiting for. */
  href: string;
}

export interface PipelineInput {
  project: Project;
  workflow: Workflow;
  sources?: readonly Source[] | undefined;
  topics?: readonly Topic[] | undefined;
  jobs?: readonly Job[] | undefined;
  scripts?: readonly Script[] | undefined;
  claims?: readonly ClaimVerification[] | undefined;
  /** Read only once the script is approved. */
  synthesis?: SynthesisStatus | undefined;
  /** Read only once the narration is complete. */
  storyboard?: StoryboardStatus | undefined;
  /** The render status the stage reports on (see `pickRenderStatus`), read once the storyboard is complete. */
  render?: RenderStatus | undefined;
  /** The project's latest render job (`null`: none yet; `undefined`: not read yet). */
  renderJob?: Job | null | undefined;
  renderProgress?: RenderProgress | null | undefined;
  /** Stages whose data could not be read, so they say so rather than wait for ever. */
  failed?: ReadonlySet<PipelineStageId> | undefined;
}

export const PIPELINE_ORDER: readonly PipelineStageId[] = [
  'source',
  'settings',
  'topics',
  'claims',
  'script',
  'narration',
  'storyboard',
  'video',
];

const LABELS: Record<PipelineStageId, string> = {
  source: 'Source',
  settings: 'Settings',
  topics: 'Topics',
  claims: 'Claims',
  script: 'Script',
  narration: 'Narration',
  storyboard: 'Storyboard',
  video: 'Video',
};

type Outcome = Pick<PipelineStage, 'status' | 'detail' | 'blocker'>;

const outcome = (
  status: PipelineStatus,
  detail: string | null = null,
  blocker: string | null = null,
): Outcome => ({ status, detail, blocker });

const blocked = (blocker: string): Outcome => outcome('blocked', null, blocker);

export function pipelineHref(projectId: string, id: PipelineStageId): string {
  const base = `/projects/${projectId}`;
  switch (id) {
    case 'source':
      return base;
    case 'settings':
      return `${base}#settings`;
    case 'topics':
      return `${base}/topics`;
    case 'claims':
      return `${base}/claims`;
    case 'script':
      return `${base}/script`;
    case 'narration':
      return `${base}/narration`;
    case 'storyboard':
      return `${base}/storyboard`;
    case 'video':
      return `${base}/video`;
  }
}

/**
 * Which of a project's render statuses the Video stage reports on. A video that is finished and
 * current beats one that merely exists, which beats a status with no video at all; among equals the
 * earlier (the final-quality one) wins.
 */
export function pickRenderStatus(
  candidates: readonly (RenderStatus | undefined)[],
): RenderStatus | undefined {
  const known = candidates.filter((status): status is RenderStatus => status !== undefined);
  return (
    known.find((status) => status.is_complete && status.output) ??
    known.find((status) => status.output) ??
    known[0]
  );
}

function sourceOutcome(input: PipelineInput): Outcome {
  const { project, sources, topics } = input;
  if (!isFileProject(project)) {
    const keywords = topics?.filter((topic) => topic.origin === 'user').length ?? 0;
    return outcome('completed', keywords > 0 ? pluralise(keywords, 'keyword') : null);
  }
  if (sources === undefined) return unresolved('source', input);
  const source = sources[0];
  if (!source) return outcome('pending', `Add a ${sourceWords(project).noun}`);
  if (source.status === 'failed') return outcome('failed', source.original_filename);
  if (source.status === 'pending' || source.status === 'processing') {
    return outcome('processing', `Reading ${source.original_filename}`);
  }
  const pages = source.page_count;
  return outcome(
    'completed',
    pages
      ? `${source.original_filename} · ${pluralise(pages, sourceWords(project).unit)}`
      : source.original_filename,
  );
}

function settingsOutcome(project: Project): Outcome {
  // The settings are stored with the project, so they are always there to read.
  return outcome(
    'completed',
    `${orientationLabel(project.orientation)} · ${formatDuration(project.target_duration_ms)} · ${languageLabel(project.language)}`,
  );
}

function topicsOutcome(input: PipelineInput, source: Outcome): Outcome {
  const { topics, jobs } = input;
  if (topics === undefined || jobs === undefined) return unresolved('topics', input);
  const extraction = latestJobOfKind(jobs, 'extract_topics');
  if (extraction && isActiveJob(extraction)) return outcome('processing', 'Extracting topics');
  const derived = topics.filter((topic) => topic.origin !== 'user');
  if (derived.length > 0 || extraction?.status === 'succeeded') {
    const usable = topics.filter((topic) => topic.status !== 'rejected');
    const accepted = topics.filter((topic) => topic.status === 'accepted').length;
    return outcome('completed', `${pluralise(usable.length, 'topic')} · ${accepted} accepted`);
  }
  if (extraction?.status === 'failed') return outcome('failed', 'Extraction did not finish');
  if (source.status !== 'completed') return blocked('The source must be read first.');
  return outcome('pending', 'Ready to extract');
}

function claimsOutcome(input: PipelineInput, topicsStage: Outcome): Outcome {
  const { claims, jobs, topics } = input;
  if (claims === undefined || jobs === undefined) return unresolved('claims', input);
  const counts = summariseClaimStatuses(claims);
  const extraction = latestJobOfKind(jobs, 'extract_claims');
  if (extraction && isActiveJob(extraction)) {
    return outcome('processing', 'Extracting and verifying claims');
  }
  if (counts.supported > 0) {
    return outcome(
      'completed',
      `${pluralise(counts.total, 'claim')} checked · ${counts.supported} supported`,
    );
  }
  if (counts.generalKnowledge > 0) {
    // A topic-only lesson has no document: its statements are general knowledge, never "supported".
    return outcome(
      'completed',
      `${pluralise(counts.generalKnowledge, 'statement')} from general knowledge · not checked against a document`,
    );
  }
  if (topicsStage.status !== 'completed') return blocked('Topics must be extracted first.');
  if (extraction?.status === 'failed' && counts.total === 0) {
    return outcome('failed', 'Verification did not finish');
  }
  if (counts.total > 0) {
    return outcome('pending', `${pluralise(counts.total, 'claim')} checked · none supported yet`);
  }
  if (topics && eligibleAcceptedTopics(topics).length === 0) {
    return outcome('pending', 'Accept a topic to verify its claims');
  }
  return outcome('pending', 'Ready to verify');
}

function scriptOutcome(input: PipelineInput, claimsStage: Outcome): Outcome {
  const { workflow, scripts } = input;
  if (scripts === undefined) return unresolved('script', input);
  const script = workflow.script;
  if (script) {
    switch (script.status) {
      case 'approved':
        return outcome(
          'completed',
          script.estimated_duration_ms
            ? `Approved · about ${formatDuration(script.estimated_duration_ms)}`
            : 'Approved',
        );
      case 'draft':
        return outcome('pending', 'Ready for your review - approve it to continue');
      case 'pending':
        return outcome('processing', 'Writing the script');
      case 'failed':
        return outcome('failed', 'Script writing did not finish');
      case 'superseded':
        break;
    }
  }
  if (claimsStage.status !== 'completed') return blocked('A verified claim is required first.');
  return outcome('pending', 'Ready to generate');
}

function narrationOutcome(input: PipelineInput, scriptStage: Outcome): Outcome {
  const { synthesis } = input;
  if (scriptStage.status !== 'completed') return blocked('Approve the script first.');
  if (synthesis === undefined) return unresolved('narration', input);
  const total = synthesis.segments.length;
  const ready = synthesis.segments.filter((segment) => segment.audio?.status === 'ready').length;
  const failed = synthesis.segments.filter((segment) => segment.audio?.status === 'failed').length;
  const job = synthesis.job;
  if (job && isActiveJob(job)) return outcome('processing', `${ready} of ${total} sections`);
  // The one gate for "narrated": every section measured (see `scripts.measured_duration_ms`).
  const measured = synthesis.script.measured_duration_ms;
  if (measured !== null) {
    return outcome('completed', `${pluralise(total, 'section')} · ${formatDuration(measured)}`);
  }
  if (failed > 0 || (job?.status === 'failed' && ready === 0)) {
    return outcome(
      'failed',
      failed > 0 ? `${pluralise(failed, 'section')} not narrated` : 'Narration did not finish',
    );
  }
  if (ready > 0) return outcome('pending', `${ready} of ${total} sections narrated`);
  return outcome('pending', 'Not narrated yet');
}

function storyboardOutcome(input: PipelineInput, narrationStage: Outcome): Outcome {
  const { storyboard } = input;
  if (narrationStage.status !== 'completed') return blocked('Complete the narration first.');
  if (storyboard === undefined) return unresolved('storyboard', input);
  const total = storyboard.segments.length;
  const scenes = storyboard.segments.reduce((sum, segment) => sum + segment.scenes.length, 0);
  const job = storyboard.job;
  if (job && isActiveJob(job)) {
    return outcome('processing', `${storyboard.ready_segment_count} of ${total} sections`);
  }
  if (storyboard.is_complete) {
    return outcome('completed', `${pluralise(scenes, 'scene')} · ${pluralise(total, 'section')}`);
  }
  if (job?.status === 'failed') return outcome('failed', 'Storyboard did not finish');
  if (storyboard.ready_segment_count > 0) {
    return outcome('pending', `${storyboard.ready_segment_count} of ${total} sections ready`);
  }
  return outcome('pending', 'Not generated yet');
}

function videoOutcome(input: PipelineInput, storyboardStage: Outcome): Outcome {
  const { render, renderJob, renderProgress } = input;
  if (storyboardStage.status !== 'completed') return blocked('Complete the storyboard first.');
  if (renderJob && isActiveJob(renderJob)) {
    const percent = renderProgress?.percent;
    return outcome('processing', percent != null ? `Rendering · ${percent}%` : 'Rendering');
  }
  if (render === undefined || renderJob === undefined) return unresolved('video', input);
  const selection = { orientation: render.orientation, quality: render.quality };
  const output = render.output;
  if (render.is_complete && output) {
    const shape = orientationLabel(render.orientation);
    const kind = render.quality === 'high' ? 'Final' : 'Preview';
    return outcome(
      'completed',
      `${kind} · ${shape} · ${output.width}×${output.height} · ${output.fps} FPS · ${formatDuration(output.duration_ms)}`,
    );
  }
  const blocker = render.blockers[0];
  if (blocker) return blocked(blocker);
  if (
    (renderJob?.status === 'failed' && jobIsFor(renderJob, selection)) ||
    render.failed_count > 0
  ) {
    return outcome('failed', 'The video did not finish');
  }
  if (output) return outcome('pending', 'Out of date - update the video');
  return outcome('pending', 'Not rendered yet');
}

/** A stage whose data has not arrived yet, or could not be read. */
function unresolved(id: PipelineStageId, input: PipelineInput): Outcome {
  return input.failed?.has(id) ? outcome('unknown', 'Could not be checked') : outcome('checking');
}

export function derivePipeline(input: PipelineInput): PipelineStage[] {
  const source = sourceOutcome(input);
  const settings = settingsOutcome(input.project);
  const topics = topicsOutcome(input, source);
  const claims = claimsOutcome(input, topics);
  const script = scriptOutcome(input, claims);
  const narration = narrationOutcome(input, script);
  const storyboard = storyboardOutcome(input, narration);
  const video = videoOutcome(input, storyboard);

  const outcomes: Record<PipelineStageId, Outcome> = {
    source,
    settings,
    topics,
    claims,
    script,
    narration,
    storyboard,
    video,
  };
  return PIPELINE_ORDER.map((id) => ({
    id,
    label: LABELS[id],
    href: pipelineHref(input.project.id, id),
    ...outcomes[id],
  }));
}
