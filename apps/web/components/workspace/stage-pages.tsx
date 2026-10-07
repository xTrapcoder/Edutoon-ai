'use client';

import {
  ArrowLeft,
  Clapperboard,
  FileVideo,
  Film,
  Gauge,
  Languages,
  Mic,
  MonitorPlay,
  Settings2,
  ShieldCheck,
  Timer,
  type LucideIcon,
} from 'lucide-react';
import { useState, type ReactNode } from 'react';
import type { ClaimVerification, Project, ScriptDetail } from '@edutoon/contracts';
import { ClaimExtractionPanel } from '@/components/topics/claim-extraction-panel';
import { RenderPanel } from '@/components/script/render-panel';
import { SegmentAudioPlayer } from '@/components/script/segment-audio-player';
import { segmentTitle, VERDICT } from '@/components/script/script-model';
import { SynthesisCard } from '@/components/script/script-studio';
import { StoryboardPanel } from '@/components/script/storyboard-panel';
import { Badge } from '@/components/ui/badge';
import { ButtonLink } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Alert, ErrorState, Skeleton } from '@/components/ui/feedback';
import {
  useRenderStatus,
  useScriptDetail,
  useSourceChunks,
  useSynthesisStatus,
} from '@/lib/api/queries';
import { formatElapsed, formatPageRange, formatTargetDuration } from '@/lib/format';
import { languageLabel, orientationLabel } from '@/lib/generation-settings';
import { PIPELINE_STATUS, type PipelineStageId } from '@/lib/pipeline';
import type { RenderSelection } from '@/lib/render';
import { QueryGate } from './query-gate';
import { useWorkspaceContext } from './workspace-context';

/** The stage each one waits on, so a blocked page can point straight at what to do first. */
const PREREQUISITE: Partial<Record<PipelineStageId, PipelineStageId>> = {
  topics: 'source',
  claims: 'topics',
  script: 'claims',
  narration: 'script',
  storyboard: 'narration',
  video: 'storyboard',
};

/**
 * The top of a stage's page: which stage this is and the status the API really reports for it. A
 * stage that is waiting on an earlier one says so, with a link to that stage.
 */
function StageBanner({
  id,
  icon: Icon,
  title,
  description,
}: {
  id: PipelineStageId;
  icon: LucideIcon;
  title: string;
  description: string;
}) {
  const { pipeline } = useWorkspaceContext();
  const stage = pipeline.find((entry) => entry.id === id);
  const prerequisiteId = PREREQUISITE[id];
  const prerequisite = prerequisiteId
    ? pipeline.find((entry) => entry.id === prerequisiteId)
    : undefined;
  if (!stage) return null;
  const status = PIPELINE_STATUS[stage.status];

  return (
    <div className="space-y-4">
      <Card>
        <CardBody className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex min-w-0 flex-1 items-start gap-3">
            <span className="mt-0.5 flex size-10 shrink-0 items-center justify-center rounded-md bg-brand-50 text-brand-700">
              <Icon className="size-5" aria-hidden />
            </span>
            <div className="min-w-0">
              <h2 className="text-lg font-semibold text-ink-950">{title}</h2>
              <p className="mt-0.5 max-w-2xl text-sm text-ink-500">{description}</p>
            </div>
          </div>
          <div className="flex shrink-0 flex-col items-start gap-1 sm:items-end">
            <Badge tone={status.tone} dot busy={stage.status === 'processing'}>
              {status.long}
            </Badge>
            {stage.detail ? (
              <p className="text-[13px] text-ink-600 sm:text-right">{stage.detail}</p>
            ) : null}
          </div>
        </CardBody>
      </Card>
      {stage.status === 'blocked' ? (
        <Alert
          tone="warning"
          title={stage.blocker ?? 'An earlier stage must be finished first.'}
          action={
            prerequisite ? (
              <ButtonLink
                href={prerequisite.href}
                variant="secondary"
                size="sm"
                leadingIcon={<ArrowLeft className="size-4" aria-hidden />}
              >
                Go to {prerequisite.label.toLowerCase()}
              </ButtonLink>
            ) : undefined
          }
        >
          This stage starts once the one before it is complete; nothing here skips that.
        </Alert>
      ) : null}
    </div>
  );
}

/**
 * Script-dependent stages (narration, storyboard, video) show their panel only once there is an
 * approved script to build on; until then the banner above says what is missing.
 */
function ApprovedScript({
  children,
}: {
  children: (script: ScriptDetail, project: Project) => ReactNode;
}) {
  const { project, workflow } = useWorkspaceContext();
  const script = workflow.script;
  const detail = useScriptDetail(project.id, script?.status === 'approved' ? script.id : undefined);

  if (script?.status !== 'approved') return null;
  if (detail.isPending) return <Skeleton className="h-64 w-full" aria-busy="true" />;
  if (detail.isError) {
    return (
      <ErrorState
        error={detail.error}
        action="load the script"
        onRetry={() => void detail.refetch()}
        retrying={detail.isRefetching}
      />
    );
  }
  return <>{children(detail.data, project)}</>;
}

// --- claims -----------------------------------------------------------------------------

const EXCERPT_CHARS = 300;

/** One cited passage of the source: where it is, how close a match it was, and the text itself. */
function CitedPassage({
  pages,
  similarity,
  text,
}: {
  pages: string | null;
  similarity: number;
  text: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const long = text.length > EXCERPT_CHARS;
  const shown = expanded || !long ? text : `${text.slice(0, EXCERPT_CHARS).trimEnd()}…`;
  const match = Math.round(Math.min(Math.max(similarity, 0), 1) * 100);

  return (
    <figure className="rounded-md border-l-4 border-brand-300 bg-brand-50/50 p-3">
      <blockquote className="text-[13px] leading-5 text-ink-700">{shown}</blockquote>
      <figcaption className="mt-1.5 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-xs text-ink-500">
        <span>
          {pages ?? 'From your document'} · {match}% match
        </span>
        {long ? (
          <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            aria-expanded={expanded}
            className="font-semibold text-brand-700 hover:underline"
          >
            {expanded ? 'Show less' : 'Show more'}
          </button>
        ) : null}
      </figcaption>
    </figure>
  );
}

/**
 * The passages of the source a claim was checked against, best match first. A citation is only a
 * pointer (a chunk and a similarity), so the text comes from the source's chunks, as on the topics
 * screen; when it cannot be read the claim still says how many passages it cites.
 */
function ClaimEvidence({
  projectId,
  sourceId,
  citations,
}: {
  projectId: string;
  sourceId: string | undefined;
  citations: ClaimVerification['citations'];
}) {
  const chunks = useSourceChunks(projectId, sourceId);
  const [showAll, setShowAll] = useState(false);
  const count = citations.length;

  if (count === 0) {
    return <p className="mt-3 text-xs text-ink-500">No source passage was cited for this claim.</p>;
  }
  const cited = count === 1 ? '1 source passage' : `${count} source passages`;
  if (sourceId !== undefined && chunks.isPending) return <Skeleton className="mt-3 h-14 w-full" />;

  const passages = [...citations]
    .sort((a, b) => b.similarity - a.similarity)
    .flatMap((citation) => {
      const chunk = chunks.data?.find((candidate) => candidate.id === citation.source_chunk_id);
      return chunk ? [{ citation, chunk }] : [];
    });
  if (passages.length === 0) {
    return (
      <p className="mt-3 text-xs text-ink-500">
        {cited} cited; the text could not be loaded just now.
      </p>
    );
  }

  const visible = showAll ? passages : passages.slice(0, 1);
  return (
    <div className="mt-3 space-y-2">
      <p className="text-xs font-semibold text-ink-700">Evidence · {cited}</p>
      {visible.map(({ citation, chunk }) => (
        <CitedPassage
          key={citation.id}
          pages={formatPageRange(chunk.page_from, chunk.page_to)}
          similarity={citation.similarity}
          text={chunk.content.replace(/\s+/g, ' ').trim()}
        />
      ))}
      {passages.length > 1 ? (
        <button
          type="button"
          onClick={() => setShowAll((value) => !value)}
          aria-expanded={showAll}
          className="text-xs font-semibold text-brand-700 hover:underline"
        >
          {showAll ? 'Show the best match only' : `Show all ${passages.length} passages`}
        </button>
      ) : null}
    </div>
  );
}

export function ClaimsPage() {
  const { project, topics, jobs, claims, sources, scripts } = useWorkspaceContext();
  const topicList = topics.data ?? [];
  const claimList = claims.data ?? [];
  const sourceId = sources.data?.[0]?.id;

  return (
    <QueryGate
      queries={[sources, topics, jobs, scripts, claims]}
      action="load your claims"
      label="Loading claims"
    >
      <div className="space-y-6">
        <StageBanner
          id="claims"
          icon={ShieldCheck}
          title="Claims and verification"
          description="Each accepted topic becomes candidate claims, and every one is checked against your source. Only supported claims can reach the script."
        />
        <ClaimExtractionPanel
          projectId={project.id}
          topics={topicList}
          claims={claimList}
          jobs={jobs.data ?? []}
          onRefreshJobs={() => void jobs.refetch()}
          refreshingJobs={jobs.isRefetching}
        />
        <Card>
          <CardHeader
            title="Verified claims"
            description="What the API holds for this project, with its verdict and the source passages it was checked against."
            icon={<ShieldCheck className="size-4" aria-hidden />}
          />
          <CardBody>
            {claimList.length === 0 ? (
              <p className="text-sm text-ink-500">No claims have been verified yet.</p>
            ) : (
              <ul className="space-y-3">
                {claimList.map((claim) => {
                  const verdict = VERDICT[claim.status];
                  const topic = claim.topic_id
                    ? topicList.find((candidate) => candidate.id === claim.topic_id)
                    : undefined;
                  return (
                    <li
                      key={claim.id}
                      className="rounded-lg border border-line bg-surface-sunken p-4"
                    >
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <p className="min-w-0 flex-1 text-sm font-medium text-ink-950">
                          {claim.claim_text}
                        </p>
                        <Badge tone={verdict.tone} dot>
                          {verdict.label}
                        </Badge>
                      </div>
                      {claim.rationale ? (
                        <p className="mt-2 text-[13px] leading-5 text-ink-600">{claim.rationale}</p>
                      ) : null}
                      {topic ? (
                        <p className="mt-2 text-xs text-ink-500">Topic: {topic.title}</p>
                      ) : null}
                      <ClaimEvidence
                        projectId={project.id}
                        sourceId={sourceId}
                        citations={claim.citations}
                      />
                    </li>
                  );
                })}
              </ul>
            )}
          </CardBody>
        </Card>
      </div>
    </QueryGate>
  );
}

// --- narration --------------------------------------------------------------------------

function NarrationSections({ project, script }: { project: Project; script: ScriptDetail }) {
  const { topics } = useWorkspaceContext();
  const { data: status } = useSynthesisStatus(project.id, script.id);
  const audioBySegment = new Map(
    (status?.segments ?? []).map((segment) => [segment.id, segment.audio] as const),
  );

  return (
    <Card>
      <CardHeader
        title="Narration by section"
        description="Each section is narrated separately; play or download the audio the API has stored."
        icon={<Mic className="size-4" aria-hidden />}
      />
      <CardBody>
        <ol className="space-y-4">
          {script.segments.map((segment) => {
            const audio = audioBySegment.get(segment.id);
            return (
              <li key={segment.id} className="rounded-lg border border-line bg-surface-sunken p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-sm font-semibold text-ink-950">
                    {segment.segment_index + 1}. {segmentTitle(segment, topics.data ?? [])}
                  </p>
                  {audio?.status === 'ready' && audio.measured_duration_ms !== null ? (
                    <Badge tone="success" dot>
                      {formatElapsed(audio.measured_duration_ms)} narrated
                    </Badge>
                  ) : audio?.status === 'failed' ? (
                    <Badge tone="danger" dot>
                      Narration failed
                    </Badge>
                  ) : (
                    <Badge tone="neutral" dot>
                      Not narrated yet
                    </Badge>
                  )}
                </div>
                {audio?.status === 'ready' ? (
                  <div className="mt-3">
                    <SegmentAudioPlayer
                      projectId={project.id}
                      scriptId={script.id}
                      segmentId={segment.id}
                      downloadName={`narration-section-${segment.segment_index + 1}.${audio.format}`}
                    />
                  </div>
                ) : null}
              </li>
            );
          })}
        </ol>
      </CardBody>
    </Card>
  );
}

export function NarrationPage() {
  const { topics, scripts } = useWorkspaceContext();
  return (
    <QueryGate queries={[topics, scripts]} action="load the narration" label="Loading narration">
      <div className="space-y-6">
        <StageBanner
          id="narration"
          icon={Mic}
          title="Narration"
          description="The approved script, spoken section by section with a local, offline voice. Its measured length decides the timing of everything after it."
        />
        <ApprovedScript>
          {(script, project) => (
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
              <NarrationSections project={project} script={script} />
              <div>
                <SynthesisCard project={project} script={script} />
              </div>
            </div>
          )}
        </ApprovedScript>
      </div>
    </QueryGate>
  );
}

// --- storyboard -------------------------------------------------------------------------

export function StoryboardPage() {
  const { topics, scripts } = useWorkspaceContext();
  return (
    <QueryGate queries={[topics, scripts]} action="load the storyboard" label="Loading storyboard">
      <div className="space-y-6">
        <StageBanner
          id="storyboard"
          icon={Clapperboard}
          title="Storyboard"
          description="A timed, templated scene timeline built from the measured narration. Every scene's wording comes from its section's own narration."
        />
        <ApprovedScript>
          {(script, project) => (
            <StoryboardPanel
              project={project}
              script={script}
              renderSelection={{ orientation: project.orientation, quality: 'low' }}
            />
          )}
        </ApprovedScript>
      </div>
    </QueryGate>
  );
}

// --- video ------------------------------------------------------------------------------

/**
 * The project's real video settings. Orientation, length and language are stored on the project;
 * the frame rate is only shown once the API has reported one (with a render estimate or a finished
 * video), never assumed.
 */
function VideoSettingsCard({
  project,
  script,
  selection,
}: {
  project: Project;
  script: ScriptDetail;
  selection: RenderSelection;
}) {
  const { pipeline } = useWorkspaceContext();
  const narrated = pipeline.find((stage) => stage.id === 'narration')?.status === 'completed';
  const { data: render } = useRenderStatus(
    project.id,
    narrated ? script.id : undefined,
    selection.orientation,
    selection.quality,
  );
  const fps = render?.output?.fps ?? render?.estimate?.fps;

  const rows: { icon: LucideIcon; term: string; value: string }[] = [
    { icon: MonitorPlay, term: 'Orientation', value: orientationLabel(project.orientation) },
    {
      icon: Timer,
      term: 'Target duration',
      value: formatTargetDuration(project.target_duration_ms),
    },
    { icon: Languages, term: 'Language', value: languageLabel(project.language) },
  ];
  if (fps !== undefined) rows.push({ icon: Gauge, term: 'Frame rate', value: `${fps} FPS` });
  if (render?.output) {
    rows.push({
      icon: FileVideo,
      term: 'Encoding',
      value: `${render.output.encoder} · ${render.output.width}×${render.output.height}`,
    });
  }

  return (
    <Card>
      <CardHeader
        title="Video settings"
        description="From this project; change them from the project overview."
        icon={<Settings2 className="size-4" aria-hidden />}
      />
      <CardBody>
        <dl className="divide-y divide-line">
          {rows.map((row) => (
            <div
              key={row.term}
              className="flex items-center justify-between gap-4 py-2.5 first:pt-0 last:pb-0"
            >
              <dt className="flex items-center gap-2 text-sm text-ink-500">
                <row.icon className="size-4" aria-hidden />
                {row.term}
              </dt>
              <dd className="text-sm font-semibold text-ink-950">{row.value}</dd>
            </div>
          ))}
        </dl>
      </CardBody>
    </Card>
  );
}

function VideoWorkspace({ project, script }: { project: Project; script: ScriptDetail }) {
  const [selection, setSelection] = useState<RenderSelection>({
    orientation: project.orientation,
    quality: 'low',
  });
  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
      <RenderPanel
        project={project}
        script={script}
        selection={selection}
        onSelectionChange={setSelection}
      />
      <div>
        <VideoSettingsCard project={project} script={script} selection={selection} />
      </div>
    </div>
  );
}

export function VideoPage() {
  const { topics, scripts } = useWorkspaceContext();
  return (
    <QueryGate queries={[topics, scripts]} action="load the video" label="Loading video">
      <div className="space-y-6">
        <StageBanner
          id="video"
          icon={Film}
          title="Video"
          description="The approved script, its narration and its storyboard rendered to an MP4. Rendering needs the earlier stages complete; nothing here bypasses that."
        />
        <ApprovedScript>
          {(script, project) => <VideoWorkspace project={project} script={script} />}
        </ApprovedScript>
      </div>
    </QueryGate>
  );
}
