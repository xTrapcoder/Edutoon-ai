'use client';

import { useQueryClient } from '@tanstack/react-query';
import { Download, Film, RefreshCw } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { Project, RenderStatus, ScriptDetail } from '@edutoon/contracts';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Alert, Progress } from '@/components/ui/feedback';
import { describeError } from '@/lib/api/errors';
import { fetchRenderVideoUrl } from '@/lib/api/endpoints';
import {
  queryKeys,
  useEnqueueRender,
  useRenderProgress,
  useRenderStatus,
  useRetryRender,
  useStoryboardStatus,
  useSynthesisStatus,
} from '@/lib/api/queries';
import { cn } from '@/lib/cn';
import { formatBytes, formatDuration } from '@/lib/format';
import { isActiveJob } from '@/lib/jobs';
import {
  describeRenderProgress,
  derivePhase,
  jobIsFor,
  ORIENTATION_CHOICES,
  QUALITY_CHOICES,
  renderButtonLabel,
  renderFailureMessage,
  summariseScenes,
  type RenderSelection,
} from '@/lib/render';

interface RenderPanelProps {
  project: Project;
  script: ScriptDetail;
  selection: RenderSelection;
  onSelectionChange: (selection: RenderSelection) => void;
}

interface SegmentedProps<T extends string> {
  legend: string;
  name: string;
  value: T;
  choices: readonly { value: T; title: string; detail: string }[];
  disabled: boolean;
  onChange: (value: T) => void;
}

/** A small either/or choice drawn as joined buttons; a real radio group underneath. */
function Segmented<T extends string>({
  legend,
  name,
  value,
  choices,
  disabled,
  onChange,
}: SegmentedProps<T>) {
  const current = choices.find((choice) => choice.value === value);
  return (
    <fieldset className="min-w-0 space-y-1.5" disabled={disabled}>
      <legend className="text-[13px] font-medium text-ink-700">{legend}</legend>
      <div className="grid grid-cols-2 gap-2">
        {choices.map((choice) => (
          <label
            key={choice.value}
            className={cn(
              'flex min-h-10 cursor-pointer items-center justify-center rounded-md border px-2 text-center text-sm font-semibold transition-colors has-focus-visible:outline-2 has-focus-visible:outline-offset-2 has-focus-visible:outline-brand-500 has-disabled:cursor-not-allowed has-disabled:opacity-60',
              choice.value === value
                ? 'border-brand-600 bg-brand-50 text-brand-800 shadow-[0_0_0_1px_var(--color-brand-600)]'
                : 'border-line-strong bg-surface text-ink-700 hover:border-brand-300',
            )}
          >
            <input
              type="radio"
              name={name}
              value={choice.value}
              checked={choice.value === value}
              onChange={() => onChange(choice.value)}
              className="sr-only"
            />
            {choice.title}
          </label>
        ))}
      </div>
      {current ? <p className="text-[13px] text-ink-500">{current.detail}</p> : null}
    </fieldset>
  );
}

interface RenderedVideoProps {
  projectId: string;
  scriptId: string;
  selection: RenderSelection;
  /** Changes whenever the stored video does, so a newer one is fetched. */
  version: string;
  label: string;
}

/**
 * The finished video, played from a `blob:` URL fetched with the caller's own token (a plain
 * `<video src>` cannot carry an `Authorization` header - the same reason narration audio works
 * this way). Always revokes the URL it created.
 */
function RenderedVideo({ projectId, scriptId, selection, version, label }: RenderedVideoProps) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    setUrl(null);
    setFailed(false);
    fetchRenderVideoUrl(projectId, scriptId, selection.orientation, selection.quality)
      .then((fetched) => {
        if (cancelled) {
          URL.revokeObjectURL(fetched);
          return;
        }
        objectUrl = fetched;
        setUrl(fetched);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [projectId, scriptId, selection.orientation, selection.quality, version]);

  if (failed) {
    return <p className="text-[13px] text-danger-600">This video couldn&apos;t be loaded.</p>;
  }
  const vertical = selection.orientation === 'vertical';
  return (
    <div className="space-y-2">
      <p className="text-[13px] font-medium text-ink-700">{label}</p>
      {url ? (
        <>
          <video
            controls
            preload="metadata"
            src={url}
            aria-label={label}
            className={cn(
              'w-full rounded-md bg-ink-950',
              vertical ? 'mx-auto aspect-[9/16] max-h-[70vh] max-w-[260px]' : 'aspect-video',
            )}
          >
            Your browser does not support the video element.
          </video>
          <a
            href={url}
            download={`video-${selection.orientation}-${selection.quality}.mp4`}
            className="inline-flex h-8 items-center gap-2 rounded-md border border-line-strong bg-surface px-3 text-[13px] font-semibold text-ink-800 shadow-xs hover:border-ink-300 hover:bg-surface-sunken"
          >
            <Download className="size-4" aria-hidden />
            Download MP4
          </a>
        </>
      ) : (
        <p className="text-[13px] text-ink-500">Loading video…</p>
      )}
    </div>
  );
}

function estimateLine(status: RenderStatus): string | null {
  const estimate = status.estimate;
  if (!estimate) return null;
  const parts = [`${formatDuration(estimate.total_ms)} of video`];
  if (estimate.chunks_to_render === 0) {
    parts.push('every scene is already rendered');
  } else {
    parts.push(
      `${estimate.chunks_to_render} of ${estimate.unique_chunk_count} scenes to render` +
        (estimate.chunks_reusable > 0 ? ` (${estimate.chunks_reusable} reused)` : ''),
    );
  }
  parts.push(`about ${formatBytes(estimate.estimated_output_bytes)}`);
  return parts.join(' · ');
}

/**
 * Phase 10's video rendering, inside the existing Script Studio (approved-script stage) - never
 * a separate workflow. Offered once narration is fully measured; the storyboard panel beside it
 * shows each scene's render state, so a change to one scene is visibly a change to that scene.
 */
export function RenderPanel({ project, script, selection, onSelectionChange }: RenderPanelProps) {
  const approved = script.status === 'approved';
  const queryClient = useQueryClient();

  const { data: synthesisStatus } = useSynthesisStatus(
    project.id,
    approved ? script.id : undefined,
  );
  const measuredDurationMs =
    synthesisStatus?.script.measured_duration_ms ?? script.measured_duration_ms;
  const narrationReady = measuredDurationMs !== null;
  const active = approved && narrationReady;

  const { data: storyboard } = useStoryboardStatus(project.id, active ? script.id : undefined);
  const statusQuery = useRenderStatus(
    project.id,
    active ? script.id : undefined,
    selection.orientation,
    selection.quality,
  );
  const status = statusQuery.data;
  const progressQuery = useRenderProgress(project.id, active);
  const enqueue = useEnqueueRender(project.id, script.id);
  const retry = useRetryRender(project.id, script.id);

  // The freshest view of the latest render job: the cheap poll while it runs, the full status otherwise.
  const job = progressQuery.data?.job ?? status?.job ?? null;
  const running = job !== null && isActiveJob(job);

  // Read the full status again whenever something it depends on settles: the render job itself,
  // narration, or the storyboard (any of which can make scenes stale or a video out of date).
  const signature = [
    job ? `${job.id}:${job.status}` : '',
    synthesisStatus?.job ? `${synthesisStatus.job.id}:${synthesisStatus.job.status}` : '',
    storyboard?.job ? `${storyboard.job.id}:${storyboard.job.status}` : '',
    storyboard ? String(storyboard.is_complete) : '',
    String(measuredDurationMs),
  ].join('|');
  const previous = useRef<string | null>(null);
  useEffect(() => {
    if (previous.current !== null && previous.current !== signature) {
      void queryClient.invalidateQueries({ queryKey: queryKeys.renderAll(project.id, script.id) });
    }
    previous.current = signature;
  }, [signature, queryClient, project.id, script.id]);

  if (!approved) return null;

  if (!narrationReady) {
    return (
      <Card id="video" className="scroll-mt-24">
        <CardHeader title="Video" icon={<Film className="size-4" aria-hidden />} as="h2" />
        <CardBody>
          <p className="text-sm text-ink-600">
            The video is timed to the narration, so narrate every section before rendering.
          </p>
        </CardBody>
      </Card>
    );
  }

  const phase = derivePhase(status, job, selection);
  const progress = running ? describeRenderProgress(progressQuery.data?.progress) : null;
  const summary = status ? summariseScenes(status) : [];
  const blocker = status?.blockers[0];
  const jobForSelection = jobIsFor(job, selection) ? job : null;
  const canRetry = phase === 'failed' && job?.status === 'failed' && jobIsFor(job, selection);
  const mutation = canRetry ? retry : enqueue;
  const busy = running || enqueue.isPending || retry.isPending;
  const error = enqueue.isError
    ? describeError(enqueue.error, { action: 'start the render' })
    : retry.isError
      ? describeError(retry.error, { action: 'retry the render' })
      : null;

  const failureTitle = summary[0]?.text ?? 'The video is not finished';
  const failureMessage = renderFailureMessage(jobForSelection ?? job);
  // The job's own message usually begins with the same sentence as the title: say it once.
  const failureBody = failureMessage.startsWith(failureTitle)
    ? failureMessage.slice(failureTitle.length).replace(/^[.\s]+/, '')
    : failureMessage;

  function start() {
    if (canRetry) retry.mutate();
    else enqueue.mutate(selection);
  }

  const counts = status
    ? [
        { key: 'ready', tone: 'success' as const, value: status.ready_count, label: 'ready' },
        {
          key: 'needs',
          tone: 'info' as const,
          value: status.needs_render_count,
          label: 'to render',
        },
        { key: 'stale', tone: 'warning' as const, value: status.stale_count, label: 'stale' },
        { key: 'failed', tone: 'danger' as const, value: status.failed_count, label: 'failed' },
      ].filter((entry) => entry.value > 0)
    : [];

  return (
    <Card id="video" className="scroll-mt-24">
      <CardHeader title="Video" icon={<Film className="size-4" aria-hidden />} as="h2" />
      <CardBody className="space-y-4">
        <Segmented
          legend="Shape"
          name="render-orientation"
          value={selection.orientation}
          choices={ORIENTATION_CHOICES}
          disabled={busy}
          onChange={(orientation) => onSelectionChange({ ...selection, orientation })}
        />
        <Segmented
          legend="Quality"
          name="render-quality"
          value={selection.quality}
          choices={QUALITY_CHOICES}
          disabled={busy}
          onChange={(quality) => onSelectionChange({ ...selection, quality })}
        />

        {phase === 'active' && progress ? (
          <div className="space-y-2" aria-live="polite">
            <Badge tone="brand" dot busy>
              {progress.headline}
            </Badge>
            <Progress value={progress.percent} label="Render progress" />
            {progress.percent !== undefined ? (
              <p className="text-[13px] font-medium text-ink-700">{progress.percent}%</p>
            ) : null}
            {progress.details.map((line) => (
              <p key={line} className="text-[13px] text-ink-500">
                {line}
              </p>
            ))}
            <p className="text-[13px] text-ink-500">
              You can leave this page - the video keeps rendering.
            </p>
          </div>
        ) : null}

        {phase === 'blocked' && blocker ? (
          <Alert tone="warning" title="Not ready to render yet">
            {blocker}
          </Alert>
        ) : null}

        {phase === 'failed' ? (
          <Alert tone="danger" title={failureTitle}>
            {failureBody}
          </Alert>
        ) : null}

        {phase === 'outdated' ? (
          <Alert tone="warning" title="This video is out of date">
            {summary.map((line) => line.text).join('. ') || 'The script has changed.'}
          </Alert>
        ) : null}

        {phase === 'not_rendered' && status && !running ? (
          <p className="text-sm text-ink-600">
            {selection.quality === 'low'
              ? 'Make a quick preview to check the timing and look.'
              : 'Render the full-size video.'}
          </p>
        ) : null}

        {phase === 'complete' && status?.output ? (
          <div className="space-y-1">
            <Badge tone="success" dot>
              Video: Up to date
            </Badge>
            <p className="text-[13px] text-ink-500">
              {status.output.width} × {status.output.height} ·{' '}
              {formatDuration(status.output.duration_ms)} · {formatBytes(status.output.byte_size)}
            </p>
          </div>
        ) : null}

        {counts.length > 0 && phase !== 'blocked' ? (
          <div className="flex flex-wrap gap-1.5" aria-label="Scene render states">
            {counts.map((entry) => (
              <Badge key={entry.key} tone={entry.tone}>
                {entry.value} {entry.label}
              </Badge>
            ))}
          </div>
        ) : null}

        {status && phase !== 'blocked' && phase !== 'active' && phase !== 'complete' ? (
          <p className="text-[13px] text-ink-500">{estimateLine(status)}</p>
        ) : null}

        {error ? (
          <Alert tone="danger" title={error.title}>
            {error.message}
          </Alert>
        ) : null}

        {!running ? (
          <Button
            className="w-full"
            variant={phase === 'complete' ? 'secondary' : 'primary'}
            onClick={start}
            loading={mutation.isPending}
            disabled={phase === 'blocked' || statusQuery.isPending}
            leadingIcon={<RefreshCw className="size-4" aria-hidden />}
          >
            {renderButtonLabel(phase, selection.quality, status)}
          </Button>
        ) : null}

        {status?.output && (phase === 'complete' || phase === 'outdated') ? (
          <div className="border-t border-line pt-4">
            <RenderedVideo
              projectId={project.id}
              scriptId={script.id}
              selection={selection}
              version={status.output.updated_at}
              label={phase === 'complete' ? 'Your video' : 'Previous version'}
            />
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}
