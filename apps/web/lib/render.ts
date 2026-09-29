import type {
  Job,
  Orientation,
  RenderProgress,
  RenderQuality,
  RenderSceneState,
  RenderStatus,
} from '@edutoon/contracts';
import { safeServerMessage } from '@/lib/api/errors';
import { formatDuration, pluralise } from '@/lib/format';
import { isActiveJob } from '@/lib/jobs';
import type { Tone } from '@/lib/workflow';

/** The two shapes and two qualities a person picks between, described in words they can act on. */
export const ORIENTATION_CHOICES: readonly {
  value: Orientation;
  title: string;
  detail: string;
}[] = [
  { value: 'horizontal', title: 'Horizontal', detail: '1920 × 1080 — for screens and slides' },
  { value: 'vertical', title: 'Vertical', detail: '1080 × 1920 — for phones' },
];

export const QUALITY_CHOICES: readonly { value: RenderQuality; title: string; detail: string }[] = [
  { value: 'low', title: 'Preview', detail: 'Half size, quick to make — to check the video' },
  { value: 'high', title: 'Final', detail: 'Full size — the video to keep and share' },
];

export interface RenderSelection {
  orientation: Orientation;
  quality: RenderQuality;
}

/** What the panel is showing, decided in one place so an unfinished video is never called finished. */
export type RenderPhase =
  | 'blocked' // something upstream (narration, storyboard) is not ready
  | 'active' // a render is queued or running
  | 'failed' // the last render for this selection stopped before finishing
  | 'complete' // a video exists and is exactly what the script and storyboard say now
  | 'outdated' // a video exists but scenes have changed since
  | 'not_rendered'; // nothing has been rendered for this selection yet

/** Whether a render job was for this orientation and quality (a job for another shape says nothing here). */
export function jobIsFor(job: Job | null | undefined, selection: RenderSelection): boolean {
  if (!job) return false;
  const payload = job.payload as Record<string, unknown> | null;
  return payload?.orientation === selection.orientation && payload?.quality === selection.quality;
}

export function derivePhase(
  status: RenderStatus | undefined,
  job: Job | null | undefined,
  selection: RenderSelection,
): RenderPhase {
  if (job && isActiveJob(job)) return 'active';
  if (!status) return 'not_rendered';
  if (status.blockers.length > 0) return 'blocked';
  if (status.is_complete) return 'complete';
  const failedJob = job?.status === 'failed' && jobIsFor(job, selection);
  if (failedJob || status.failed_count > 0) return 'failed';
  if (status.output) return 'outdated';
  return 'not_rendered';
}

const STAGE_LABELS: Record<string, string> = {
  preparing: 'Preparing to render',
  checking_stored_sections: 'Checking what is already rendered',
  rendering: 'Rendering video',
  assembling: 'Putting the video together',
  verifying: 'Checking the finished video',
  publishing: 'Saving the video',
  finalising: 'Finishing up',
};

export interface RenderProgressView {
  headline: string;
  /** One short line per fact that is actually known; never a guess. */
  details: string[];
  /** 0–100, only when the worker has reported it. */
  percent: number | undefined;
}

/** The progress of a running render as the person reads it: "Section 7 of 37 · Scene 3 of 5 ...". */
export function describeRenderProgress(
  progress: RenderProgress | null | undefined,
): RenderProgressView {
  if (!progress?.stage) {
    return { headline: 'Waiting for a free renderer', details: [], percent: undefined };
  }
  const details: string[] = [];
  const { segment_index, segment_count, scene_index, scene_count } = progress;
  if (progress.stage === 'rendering' && segment_index != null && segment_count != null) {
    let where = `Section ${segment_index + 1} of ${segment_count}`;
    if (scene_index != null && scene_count != null) {
      where += ` · Scene ${scene_index + 1} of ${scene_count}`;
    }
    details.push(where);
  }
  if (progress.chunks_total != null && progress.chunks_complete != null) {
    const noun = progress.chunks_total === 1 ? 'scene' : 'scenes';
    details.push(`${progress.chunks_complete} of ${progress.chunks_total} ${noun} ready`);
  }
  if (progress.eta_seconds != null && progress.eta_seconds > 0) {
    details.push(`About ${formatDuration(progress.eta_seconds * 1000)} left`);
  }
  return {
    headline: STAGE_LABELS[progress.stage] ?? 'Rendering video',
    details,
    percent: progress.percent ?? undefined,
  };
}

export interface RenderSummary {
  tone: Tone;
  text: string;
}

/** The plain-words state of the scenes, most urgent first. Empty when everything is ready. */
export function summariseScenes(status: RenderStatus): RenderSummary[] {
  const lines: RenderSummary[] = [];
  const { failed_count: failed, stale_count: stale, needs_render_count: needed } = status;
  if (failed > 0) {
    lines.push({
      tone: 'danger',
      text: `${pluralise(failed, 'render section')} ${failed === 1 ? 'needs' : 'need'} another attempt`,
    });
  }
  if (stale > 0) {
    lines.push({
      tone: 'warning',
      text: `${pluralise(stale, 'scene')} changed since the last video`,
    });
  }
  if (needed > 0) {
    lines.push({
      tone: 'info',
      text: `${pluralise(needed, 'scene')} ${needed === 1 ? 'is' : 'are'} not rendered yet`,
    });
  }
  return lines;
}

export function renderButtonLabel(
  phase: RenderPhase,
  quality: RenderQuality,
  status: RenderStatus | undefined,
): string {
  const noun = quality === 'low' ? 'preview' : 'video';
  if (phase === 'failed') return 'Retry';
  if (phase === 'outdated') {
    const changed = (status?.stale_count ?? 0) + (status?.needs_render_count ?? 0);
    return changed > 0 ? `Update ${noun} (${pluralise(changed, 'scene')})` : `Update ${noun}`;
  }
  if (phase === 'complete') return `Render ${noun} again`;
  return `Render ${noun}`;
}

const SCENE_STATE_VIEW: Record<RenderSceneState, { label: string; tone: Tone }> = {
  ready: { label: 'Ready', tone: 'success' },
  needs_render: { label: 'Needs render', tone: 'info' },
  stale: { label: 'Stale', tone: 'warning' },
  failed: { label: 'Failed', tone: 'danger' },
};

export function sceneStateView(state: RenderSceneState): { label: string; tone: Tone } {
  return SCENE_STATE_VIEW[state];
}

/** A failed render job's message, safe to show (never a raw server string). */
export function renderFailureMessage(job: Job | null | undefined): string {
  return (
    safeServerMessage(job?.error) ??
    'The video could not be finished. Everything already rendered is kept — try again.'
  );
}
