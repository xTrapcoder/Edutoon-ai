'use client';

import { useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  Check,
  CircleDashed,
  Clapperboard,
  Flag,
  XCircle,
  type LucideIcon,
} from 'lucide-react';
import { useEffect, useRef } from 'react';
import type {
  Project,
  RenderSceneState,
  Scene,
  ScriptDetail,
  SegmentStoryboard,
} from '@edutoon/contracts';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Alert, Progress } from '@/components/ui/feedback';
import { describeError } from '@/lib/api/errors';
import {
  queryKeys,
  useEnqueueStoryboard,
  useRenderStatus,
  useStoryboardStatus,
  useSynthesisStatus,
} from '@/lib/api/queries';
import { formatElapsed } from '@/lib/format';
import {
  isActiveJob,
  jobFailureMessage,
  readStoryboardProgress,
  storyboardStageLabel,
} from '@/lib/jobs';

import { sceneStateView, type RenderSelection } from '@/lib/render';
import { IllustrationSummary } from './visual-summary';

interface StoryboardPanelProps {
  project: Project;
  script: ScriptDetail;
  /** Which video's render state each scene shows. Omit to show none. */
  renderSelection?: RenderSelection;
}

/** Render state per scene, keyed `segmentId:sceneIndex`. */
type RenderStates = ReadonlyMap<string, RenderSceneState>;

const RENDER_STATE_ICON: Record<RenderSceneState, LucideIcon> = {
  ready: Check,
  needs_render: CircleDashed,
  stale: AlertTriangle,
  failed: XCircle,
};

function RenderStateBadge({ state }: { state: RenderSceneState }) {
  const view = sceneStateView(state);
  const Icon = RENDER_STATE_ICON[state];
  return (
    <Badge tone={view.tone} aria-label={`Render state: ${view.label}`}>
      <Icon className="size-3" aria-hidden />
      {view.label}
    </Badge>
  );
}

const TEMPLATE_LABEL: Record<string, string> = {
  title_card: 'Title card',
  bullet_points: 'Bullet points',
  labelled_diagram: 'Labelled parts',
  split_text_illustration: 'Text + visual',
  full_bleed_illustration: 'Full-frame visual',
  formula_or_code: 'Formula / code',
};

/** A scene's `content` is a loosely-typed object whose real shape depends on its template
 * (validated server-side) - this file only ever reads a field after checking its type, and
 * silently shows nothing for a field that is missing or the wrong shape, never a crash. */
function asString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined;
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
}

function SceneContentPreview({ scene }: { scene: Scene }) {
  const content = scene.content as Record<string, unknown>;

  switch (scene.template) {
    case 'title_card':
      return (
        <div className="space-y-0.5">
          {asString(content.title) ? (
            <p className="font-semibold text-ink-950">{asString(content.title)}</p>
          ) : null}
          {asString(content.subtitle) ? (
            <p className="text-ink-600">{asString(content.subtitle)}</p>
          ) : null}
        </div>
      );

    case 'bullet_points': {
      const bullets = asStringArray(content.bullets);
      return (
        <div className="space-y-1">
          {asString(content.title) ? (
            <p className="font-semibold text-ink-950">{asString(content.title)}</p>
          ) : null}
          <ul className="list-disc space-y-0.5 pl-4 text-ink-800">
            {bullets.map((bullet, index) => (
              <li key={index}>{bullet}</li>
            ))}
          </ul>
        </div>
      );
    }

    case 'labelled_diagram': {
      const illustration = asRecord(content.illustration);
      const labels = Array.isArray(content.labels) ? content.labels : [];
      return (
        <div className="space-y-2">
          <IllustrationSummary
            content={content}
            description={asString(illustration?.description)}
          />
          {labels.length > 0 ? (
            <ul className="flex flex-wrap gap-1.5">
              {labels.map((raw, index) => {
                const label = asRecord(raw);
                const text = asString(label?.text);
                return text ? (
                  <li key={index}>
                    <Badge tone="neutral">{text}</Badge>
                  </li>
                ) : null;
              })}
            </ul>
          ) : null}
        </div>
      );
    }

    case 'split_text_illustration': {
      const illustration = asRecord(content.illustration);
      return (
        <div className="space-y-2">
          {asString(content.text) ? <p className="text-ink-800">{asString(content.text)}</p> : null}
          <IllustrationSummary
            content={content}
            description={asString(illustration?.description)}
          />
        </div>
      );
    }

    case 'full_bleed_illustration': {
      const illustration = asRecord(content.illustration);
      return (
        <div className="space-y-2">
          <IllustrationSummary
            content={content}
            description={asString(illustration?.description)}
          />
          {asString(content.caption) ? (
            <p className="text-ink-600">{asString(content.caption)}</p>
          ) : null}
        </div>
      );
    }

    case 'formula_or_code':
      return (
        <div className="space-y-1">
          {asString(content.label) ? (
            <p className="text-ink-500">{asString(content.label)}</p>
          ) : null}
          <code className="block overflow-x-auto rounded bg-surface-sunken px-2 py-1.5 font-mono text-[13px] text-ink-900">
            {asString(content.content) ?? ''}
          </code>
        </div>
      );

    default:
      return null;
  }
}

function SceneCard({ scene, renderState }: { scene: Scene; renderState?: RenderSceneState }) {
  return (
    <li className="space-y-2 rounded-lg border border-line bg-surface p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex flex-wrap items-center gap-1.5">
          <Badge tone="neutral">{TEMPLATE_LABEL[scene.template] ?? scene.template}</Badge>
          {renderState ? <RenderStateBadge state={renderState} /> : null}
        </span>
        <span className="text-[12px] text-ink-500">
          {formatElapsed(scene.start_ms)}–{formatElapsed(scene.start_ms + scene.duration_ms)} (
          {formatElapsed(scene.duration_ms)})
        </span>
      </div>
      <div className="text-[13px] leading-5">
        <SceneContentPreview scene={scene} />
      </div>
      {scene.reviewer_flag ? (
        <Badge tone="warning" dot>
          <Flag className="size-3" aria-hidden />
          Reviewer: check this against the source
        </Badge>
      ) : null}
    </li>
  );
}

function SegmentTimeline({
  segment,
  index,
  renderStates,
}: {
  segment: SegmentStoryboard;
  index: number;
  renderStates: RenderStates;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-ink-950">Section {index + 1}</h3>
        {segment.is_stale ? (
          <Badge tone="warning" dot>
            Stale — needs another attempt
          </Badge>
        ) : segment.is_ready ? (
          <Badge tone="success" dot>
            Ready
          </Badge>
        ) : (
          <Badge tone="neutral">Not yet generated</Badge>
        )}
      </div>
      {segment.scenes.length === 0 ? (
        <p className="text-[13px] text-ink-500">
          {segment.expected_scene_count === 0
            ? 'Too short for a scene of its own.'
            : 'Not yet storyboarded.'}
        </p>
      ) : (
        <ul className="space-y-2">
          {segment.scenes.map((scene) => (
            <SceneCard
              key={scene.id}
              scene={scene}
              renderState={renderStates.get(`${segment.id}:${scene.scene_index}`)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Phase 9's storyboard panel, inside the existing Script Studio (approved-script stage) - never
 * a separate workflow step. Only offered once narration is fully measured
 * (`script.measured_duration_ms`), since scene timing comes entirely from that.
 */
export function StoryboardPanel({ project, script, renderSelection }: StoryboardPanelProps) {
  const approved = script.status === 'approved';
  // The live-polled synthesis status is the freshest source for measured_duration_ms - script
  // itself is only refetched on demand, not while a narration job is running in the background
  // (mirrors ProjectSettingsCard's own reasoning in script-studio.tsx).
  const { data: synthesisStatus } = useSynthesisStatus(
    project.id,
    approved ? script.id : undefined,
  );
  const measuredDurationMs =
    synthesisStatus?.script.measured_duration_ms ?? script.measured_duration_ms;
  const narrationReady = measuredDurationMs !== null;
  const { data: status } = useStoryboardStatus(project.id, approved ? script.id : undefined);
  const enqueue = useEnqueueStoryboard(project.id, script.id);
  // Each scene's render state, so a change to one scene visibly makes only that scene stale.
  const { data: renderStatus } = useRenderStatus(
    project.id,
    approved && narrationReady ? script.id : undefined,
    renderSelection?.orientation ?? project.orientation,
    renderSelection?.quality ?? 'low',
  );
  const renderStates: RenderStates = new Map(
    renderSelection && renderStatus
      ? renderStatus.scenes.map((s) => [`${s.segment_id}:${s.scene_index}`, s.state] as const)
      : [],
  );

  // The storyboard status query only polls while its own job is active, so a synthesis job
  // finishing (the first narration, or a later re-narration that leaves existing scenes stale)
  // would otherwise go unnoticed until something else happens to refetch it - refetch it
  // whenever the synthesis job's own status changes, mirroring useWorkspace's job-signature
  // watching in lib/api/queries.ts.
  const queryClient = useQueryClient();
  const synthesisJobSignature = synthesisStatus?.job
    ? `${synthesisStatus.job.id}:${synthesisStatus.job.status}`
    : null;
  const settledSynthesisJob = useRef<string | null>(null);
  useEffect(() => {
    if (synthesisStatus === undefined) return;
    if (
      settledSynthesisJob.current !== null &&
      settledSynthesisJob.current !== synthesisJobSignature
    ) {
      void queryClient.invalidateQueries({ queryKey: queryKeys.storyboard(project.id, script.id) });
    }
    settledSynthesisJob.current = synthesisJobSignature;
  }, [synthesisJobSignature, synthesisStatus, project.id, script.id, queryClient]);

  if (!approved) return null;

  if (!narrationReady) {
    return (
      <Card>
        <CardHeader
          title="Storyboard"
          icon={<Clapperboard className="size-4" aria-hidden />}
          as="h2"
        />
        <CardBody>
          <p className="text-sm text-ink-600">
            Finish narrating this script first - the storyboard is timed from each section&apos;s
            measured length.
          </p>
        </CardBody>
      </Card>
    );
  }

  const job = status?.job ?? null;
  const segments = status?.segments ?? [];
  const total = segments.length;
  const readyCount = status?.ready_segment_count ?? 0;
  const notReadyCount = status?.not_ready_segment_ids.length ?? 0;
  const complete = status?.is_complete ?? false;
  const droppedVisuals = status?.dropped_visual_count ?? 0;
  const active = job !== null && isActiveJob(job);
  const progress = readStoryboardProgress(job ?? undefined);
  const percent = total > 0 ? Math.round((readyCount / total) * 100) : undefined;

  const enqueueError = enqueue.isError
    ? describeError(enqueue.error, { action: 'generate the storyboard' })
    : null;

  return (
    <Card>
      <CardHeader
        title="Storyboard"
        icon={<Clapperboard className="size-4" aria-hidden />}
        as="h2"
      />
      <CardBody className="space-y-4">
        {complete ? (
          <Badge tone="success" dot>
            Storyboard: Complete
          </Badge>
        ) : active ? (
          <>
            <Badge tone="brand" dot busy>
              {progress ? storyboardStageLabel(progress) : 'Building your storyboard'}
            </Badge>
            <Progress value={percent} label="Storyboard progress" />
            <p className="text-[13px] text-ink-500">
              {readyCount} of {total} sections ready so far.
            </p>
          </>
        ) : job && job.status === 'failed' && readyCount === 0 ? (
          <Alert tone="danger" title="Storyboard failed">
            {jobFailureMessage(job)}
          </Alert>
        ) : job ? (
          <Alert
            tone="warning"
            title={`Storyboard: Incomplete — ${notReadyCount} of ${total} sections need another attempt.`}
          >
            {readyCount} of {total} sections ready.
          </Alert>
        ) : (
          <p className="text-sm text-ink-600">
            Turn this script into a timed, templated scene timeline for animation.
          </p>
        )}

        {/* Never silent: a visual the model got structurally wrong is dropped and its scene is
            drawn as text, and the reviewer is told how many (never the raw specification). */}
        {!active && droppedVisuals > 0 ? (
          <Alert tone="info">
            {droppedVisuals === 1
              ? '1 visual fell back to text because its visual specification was invalid.'
              : `${droppedVisuals} visuals fell back to text because their visual specifications were invalid.`}
          </Alert>
        ) : null}

        {!active && status ? (
          <p className="text-[13px] text-ink-500">
            Estimated AI calls:{' '}
            <span className="font-medium text-ink-700">{status.estimated_provider_calls}</span>
            {status.actual_provider_calls !== null
              ? ` (used ${status.actual_provider_calls} last run)`
              : ''}
          </p>
        ) : null}

        {enqueueError ? (
          <Alert tone="danger" title={enqueueError.title}>
            {enqueueError.message}
          </Alert>
        ) : null}

        {!active ? (
          <Button
            className="w-full"
            variant={complete ? 'secondary' : 'primary'}
            onClick={() => enqueue.mutate()}
            loading={enqueue.isPending}
            leadingIcon={<Clapperboard className="size-4" aria-hidden />}
          >
            {complete
              ? 'Regenerate storyboard'
              : readyCount > 0
                ? 'Retry failed sections'
                : 'Generate storyboard'}
          </Button>
        ) : null}

        {total > 0 ? (
          <div className="space-y-5 border-t border-line pt-4">
            {/* One banner, not a dash on every row: the start times are best estimates while
                narration is stale, being made or failed, and they settle once it finishes. */}
            {status?.timings_authoritative === false &&
            segments.some((segment) => segment.scenes.length > 0) ? (
              <Alert tone="info">Timings update when narration finishes.</Alert>
            ) : null}
            {segments.map((segment, index) => (
              <SegmentTimeline
                key={segment.id}
                segment={segment}
                index={index}
                renderStates={renderStates}
              />
            ))}
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}
