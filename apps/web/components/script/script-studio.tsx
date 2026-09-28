'use client';

import {
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  Clock,
  FileText,
  Mic,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Volume2,
} from 'lucide-react';
import Link from 'next/link';
import { useState } from 'react';
import { toast } from 'sonner';
import type { Project, ScriptDetail, SegmentAudio, Topic } from '@edutoon/contracts';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Alert, Progress } from '@/components/ui/feedback';
import { Modal } from '@/components/ui/modal';
import { EditSettingsModal } from '@/components/workspace/edit-settings-modal';
import { describeError } from '@/lib/api/errors';
import {
  useApproveScript,
  useEnqueueSynthesis,
  useGenerateScript,
  useSynthesisStatus,
} from '@/lib/api/queries';
import { cn } from '@/lib/cn';
import { formatDateTime, formatDuration, formatElapsed } from '@/lib/format';
import { languageLabel, orientationLabel } from '@/lib/generation-settings';
import {
  isActiveJob,
  jobFailureMessage,
  readSynthesisProgress,
  synthesisStageLabel,
} from '@/lib/jobs';
import {
  compareDuration,
  paragraphs,
  scriptWordCount,
  segmentTitle,
  VERDICT,
} from './script-model';
import { SegmentAudioPlayer } from './segment-audio-player';

interface ScriptStudioProps {
  project: Project;
  script: ScriptDetail;
  topics: readonly Topic[];
}

// --- left: sections ----------------------------------------------------------------------

function SectionNav({
  script,
  topics,
  selected,
  onSelect,
}: {
  script: ScriptDetail;
  topics: readonly Topic[];
  selected: number;
  onSelect: (index: number) => void;
}) {
  return (
    <nav aria-label="Script sections">
      <h2 className="mb-3 hidden px-1 text-sm font-semibold text-ink-950 lg:block">Sections</h2>
      <ol className="flex gap-2 overflow-x-auto pb-2 lg:flex-col lg:gap-1 lg:overflow-visible lg:pb-0">
        {script.segments.map((segment, index) => {
          const active = index === selected;
          return (
            <li key={segment.id} className="shrink-0 lg:shrink">
              <button
                type="button"
                onClick={() => onSelect(index)}
                aria-current={active ? 'true' : undefined}
                className={cn(
                  'flex w-full min-w-44 items-center gap-3 rounded-md px-3 py-2.5 text-left transition-colors lg:min-w-0',
                  active ? 'bg-brand-50 ring-1 ring-brand-200 ring-inset' : 'hover:bg-canvas',
                )}
              >
                <span
                  className={cn(
                    'flex size-6 shrink-0 items-center justify-center rounded-md text-xs font-semibold',
                    active ? 'bg-brand-600 text-white' : 'bg-canvas text-ink-600',
                  )}
                >
                  {index + 1}
                </span>
                <span className="min-w-0">
                  <span
                    className={cn(
                      'block truncate text-sm',
                      active ? 'font-semibold text-brand-900' : 'font-medium text-ink-900',
                    )}
                  >
                    {segmentTitle(segment, topics)}
                  </span>
                  <span className="block text-xs text-ink-500">
                    {formatElapsed(segment.estimated_duration_ms)}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

// --- centre: the script --------------------------------------------------------------------

function SectionContent({
  script,
  topics,
  selected,
  onSelect,
  projectId,
  segmentAudio,
}: {
  script: ScriptDetail;
  topics: readonly Topic[];
  selected: number;
  onSelect: (index: number) => void;
  projectId: string;
  segmentAudio: SegmentAudio | undefined;
}) {
  const segment = script.segments[selected];
  if (!segment) return null;
  const topic = segment.topic_id
    ? topics.find((candidate) => candidate.id === segment.topic_id)
    : undefined;
  const total = script.segments.length;

  return (
    <Card>
      <CardBody className="space-y-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm font-medium text-ink-500">
              Section {selected + 1} of {total}
            </p>
            <h2 className="mt-0.5 text-xl font-semibold text-ink-950 break-anywhere">
              {segmentTitle(segment, topics)}
            </h2>
            {topic ? (
              <Link
                href={`/projects/${projectId}/topics`}
                className="mt-1 inline-block text-[13px] font-medium text-brand-700 hover:underline"
              >
                From your topic outline
              </Link>
            ) : null}
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Badge tone="neutral">
              <Clock className="size-3" aria-hidden />
              About {formatElapsed(segment.estimated_duration_ms)} estimated
            </Badge>
            {segmentAudio?.status === 'ready' && segmentAudio.measured_duration_ms !== null ? (
              <Badge tone="success" dot>
                {formatElapsed(segmentAudio.measured_duration_ms)} narrated
              </Badge>
            ) : segmentAudio?.status === 'failed' ? (
              <Badge tone="danger" dot>
                Narration failed
              </Badge>
            ) : null}
          </div>
        </div>

        <section aria-labelledby="narration-heading" className="space-y-3">
          <h3 id="narration-heading" className="text-sm font-semibold text-ink-950">
            Narration
          </h3>
          <div className="space-y-4 text-[15px] leading-7 text-ink-800">
            {paragraphs(segment.narration_text).map((paragraph, index) => (
              <p key={index}>{paragraph}</p>
            ))}
          </div>
          {segmentAudio?.status === 'ready' ? (
            <div className="space-y-1.5 rounded-lg border border-line bg-surface-sunken p-3">
              <SegmentAudioPlayer
                projectId={projectId}
                scriptId={script.id}
                segmentId={segment.id}
              />
              {segmentAudio.is_temporary_voice_substitute ? (
                <p className="text-[12px] text-ink-500">
                  Narrated in British English, standing in until a genuine Indian-English voice is
                  available - see the note in the narration panel.
                </p>
              ) : null}
            </div>
          ) : null}
        </section>

        <section aria-labelledby="claims-heading" className="space-y-3 border-t border-line pt-5">
          <h3
            id="claims-heading"
            className="flex items-center gap-2 text-sm font-semibold text-ink-950"
          >
            <ShieldCheck className="size-4 text-brand-600" aria-hidden />
            Checked against your source
          </h3>
          {segment.claims.length === 0 ? (
            <p className="text-sm text-ink-500">No verified claims are attached to this section.</p>
          ) : (
            <ul className="space-y-3">
              {segment.claims.map((claim) => {
                const verdict = VERDICT[claim.status];
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
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        <div className="flex items-center justify-between gap-3 border-t border-line pt-4">
          <Button
            variant="secondary"
            size="sm"
            disabled={selected === 0}
            onClick={() => onSelect(selected - 1)}
            leadingIcon={<ArrowLeft className="size-4" aria-hidden />}
          >
            Previous
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={selected === total - 1}
            onClick={() => onSelect(selected + 1)}
            trailingIcon={<ArrowRight className="size-4" aria-hidden />}
          >
            Next
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}

// --- right: settings, details and approval -------------------------------------------------

function ApprovalCard({ project, script }: { project: Project; script: ScriptDetail }) {
  const approve = useApproveScript(project.id);
  const regenerate = useGenerateScript(project.id);
  const [confirming, setConfirming] = useState<'approve' | 'regenerate' | null>(null);
  const approved = script.status === 'approved';

  const close = () => {
    if (approve.isPending || regenerate.isPending) return;
    approve.reset();
    regenerate.reset();
    setConfirming(null);
  };

  const confirmApprove = () =>
    approve.mutate(script.id, {
      onSuccess: () => {
        toast.success('Script approved');
        setConfirming(null);
      },
    });
  const confirmRegenerate = () =>
    regenerate.mutate(undefined, {
      onSuccess: () => {
        toast.success('Writing a new version');
        setConfirming(null);
      },
    });

  const approveError = approve.isError
    ? describeError(approve.error, { action: 'approve this script' })
    : null;
  const regenerateError = regenerate.isError
    ? describeError(regenerate.error, { action: 'write a new version' })
    : null;

  return (
    <>
      <Card>
        <CardHeader
          title="Approval"
          icon={<CheckCircle2 className="size-4" aria-hidden />}
          as="h2"
        />
        <CardBody className="space-y-4">
          {approved ? (
            <>
              <Badge tone="success" dot>
                Approved
              </Badge>
              <p className="text-sm text-ink-600">
                You approved this script. It is the version the next production stages will build
                on.
              </p>
            </>
          ) : (
            <>
              <Badge tone="brand" dot>
                Ready for your review
              </Badge>
              <p className="text-sm text-ink-600">
                Read each section and the sources behind it. Approve the script when you are happy
                with it.
              </p>
              <Button className="w-full" size="lg" onClick={() => setConfirming('approve')}>
                Approve script
              </Button>
              <Button
                className="w-full"
                variant="secondary"
                onClick={() => setConfirming('regenerate')}
                leadingIcon={<RefreshCw className="size-4" aria-hidden />}
              >
                Write a new version
              </Button>
              <p className="text-[13px] text-ink-500">The wording can&apos;t be edited here yet.</p>
            </>
          )}
        </CardBody>
      </Card>

      <Modal
        open={confirming === 'approve'}
        onClose={close}
        dismissible={!approve.isPending}
        size="sm"
        title="Approve this script?"
        description="Approving confirms you have reviewed the wording and the sources behind it."
        footer={
          <>
            <Button variant="secondary" onClick={close} disabled={approve.isPending}>
              Keep reviewing
            </Button>
            <Button onClick={confirmApprove} loading={approve.isPending}>
              Approve script
            </Button>
          </>
        }
      >
        {approveError ? (
          <Alert tone="danger" title={approveError.title}>
            {approveError.message}
          </Alert>
        ) : (
          <p className="text-sm text-ink-600">
            It becomes the script the later production stages are built from. Visual scenes,
            narration and rendering are not part of this step.
          </p>
        )}
      </Modal>

      <Modal
        open={confirming === 'regenerate'}
        onClose={close}
        dismissible={!regenerate.isPending}
        size="sm"
        title="Write a new version?"
        description="This draft is replaced by a new one written from your verified claims."
        footer={
          <>
            <Button variant="secondary" onClick={close} disabled={regenerate.isPending}>
              Keep this draft
            </Button>
            <Button onClick={confirmRegenerate} loading={regenerate.isPending}>
              Write new version
            </Button>
          </>
        }
      >
        {regenerateError ? (
          <Alert tone="danger" title={regenerateError.title}>
            {regenerateError.message}
          </Alert>
        ) : (
          <p className="text-sm text-ink-600">
            You will be able to review and approve the new version when it is ready.
          </p>
        )}
      </Modal>
    </>
  );
}

/**
 * Local narration synthesis (Piper - see apps/api/README.md, "Text-to-speech"). Only an approved
 * script may be narrated. en-IN projects currently narrate with the same British-English voice as
 * en-GB - a deliberate, explicit temporary substitution, never presented as Indian English.
 */
function SynthesisCard({ project, script }: { project: Project; script: ScriptDetail }) {
  const approved = script.status === 'approved';
  const { data: status } = useSynthesisStatus(project.id, approved ? script.id : undefined);
  const enqueue = useEnqueueSynthesis(project.id, script.id);

  if (!approved) return null;

  const job = status?.job ?? null;
  const segments = status?.segments ?? [];
  const total = segments.length;
  const readyCount = segments.filter((segment) => segment.audio?.status === 'ready').length;
  const failedCount = segments.filter((segment) => segment.audio?.status === 'failed').length;
  const complete = total > 0 && readyCount === total;
  const active = job !== null && isActiveJob(job);
  const progress = readSynthesisProgress(job ?? undefined);
  const percent = total > 0 ? Math.round((readyCount / total) * 100) : undefined;
  const voiceModel = segments.find((segment) => segment.audio)?.audio?.voice_model ?? null;
  const isTemporarySubstitute = segments.some(
    (segment) => segment.audio?.is_temporary_voice_substitute,
  );

  const enqueueError = enqueue.isError
    ? describeError(enqueue.error, { action: 'start narration' })
    : null;

  return (
    <Card>
      <CardHeader title="Narration" icon={<Volume2 className="size-4" aria-hidden />} as="h2" />
      <CardBody className="space-y-4">
        {project.language === 'en-IN' ? (
          <Alert tone="info">
            This project&apos;s language is Indian English. A genuine Indian-English voice
            isn&apos;t available in the local narration engine yet, so narration temporarily uses
            the same British-English voice as en-GB projects.
          </Alert>
        ) : null}

        {complete ? (
          <>
            <Badge tone="success" dot>
              All sections narrated
            </Badge>
            {status?.script.measured_duration_ms ? (
              <p className="text-sm text-ink-600">
                Actual runtime:{' '}
                <span className="font-semibold text-ink-950">
                  {formatDuration(status.script.measured_duration_ms)}
                </span>
              </p>
            ) : null}
          </>
        ) : active ? (
          <>
            <Badge tone="brand" dot busy>
              {progress ? synthesisStageLabel(progress) : 'Narrating your script'}
            </Badge>
            <Progress value={percent} label="Narration progress" />
            <p className="text-[13px] text-ink-500">
              {readyCount} of {total} sections narrated so far.
            </p>
          </>
        ) : job && job.status === 'failed' && readyCount === 0 ? (
          <Alert tone="danger" title="Narration failed">
            {jobFailureMessage(job)}
          </Alert>
        ) : failedCount > 0 ? (
          <Alert
            tone="warning"
            title={`${failedCount} section${failedCount === 1 ? '' : 's'} couldn't be narrated`}
          >
            {readyCount} of {total} sections narrated. Retrying only narrates what is missing.
          </Alert>
        ) : (
          <p className="text-sm text-ink-600">
            Turn this script into narrated audio, section by section, using a local, offline voice.
          </p>
        )}

        {voiceModel ? (
          <p className="text-[13px] text-ink-500">
            Voice: <span className="font-medium text-ink-700">{voiceModel}</span>
            {isTemporarySubstitute ? ' (temporary British-English stand-in)' : ''}
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
            leadingIcon={<Mic className="size-4" aria-hidden />}
          >
            {complete
              ? 'Re-narrate this script'
              : readyCount > 0
                ? 'Retry remaining sections'
                : 'Narrate this script'}
          </Button>
        ) : null}
      </CardBody>
    </Card>
  );
}

function ProjectSettingsCard({ project, script }: { project: Project; script: ScriptDetail }) {
  const [editing, setEditing] = useState(false);
  const comparison = compareDuration(script.estimated_duration_ms, project.target_duration_ms);
  // The live-polled synthesis status is the freshest source for measured_duration_ms - script
  // itself is only refetched on demand, not while a narration job is running.
  const { data: status } = useSynthesisStatus(
    project.id,
    script.status === 'approved' ? script.id : undefined,
  );
  const measuredDurationMs = status?.script.measured_duration_ms ?? script.measured_duration_ms;

  const rows: [string, string][] = [
    ['Orientation', orientationLabel(project.orientation)],
    ['Target duration', formatDuration(project.target_duration_ms)],
    ['Language', languageLabel(project.language)],
  ];

  return (
    <>
      <Card>
        <CardHeader
          title="Project settings"
          icon={<Settings2 className="size-4" aria-hidden />}
          as="h2"
          action={
            <Button size="sm" variant="secondary" onClick={() => setEditing(true)}>
              Edit
            </Button>
          }
        />
        <CardBody className="space-y-4">
          <dl className="divide-y divide-line">
            {rows.map(([term, value]) => (
              <div
                key={term}
                className="flex items-center justify-between gap-4 py-2.5 first:pt-0 last:pb-0"
              >
                <dt className="text-sm text-ink-500">{term}</dt>
                <dd className="text-sm font-semibold text-ink-950">{value}</dd>
              </div>
            ))}
          </dl>
          {script.estimated_duration_ms ? (
            <div className="rounded-md bg-brand-50/60 p-3 text-[13px] text-ink-600">
              Estimated (before narration):{' '}
              <span className="font-semibold text-ink-950">
                {formatDuration(script.estimated_duration_ms)}
              </span>
              {comparison?.note ? ` - ${comparison.note.toLowerCase()}` : ', close to your target'}.
            </div>
          ) : null}
          {measuredDurationMs ? (
            <div className="rounded-md bg-success-50 p-3 text-[13px] text-ink-600">
              Actual narrated runtime:{' '}
              <span className="font-semibold text-ink-950">
                {formatDuration(measuredDurationMs)}
              </span>
              .
            </div>
          ) : null}
        </CardBody>
      </Card>
      {editing ? (
        <EditSettingsModal project={project} open onClose={() => setEditing(false)} />
      ) : null}
    </>
  );
}

function DetailsCard({ script }: { script: ScriptDetail }) {
  const claims = script.segments.reduce((total, segment) => total + segment.claims.length, 0);
  const rows: [string, string][] = [
    ['Sections', String(script.segments.length)],
    ['Words', scriptWordCount(script).toLocaleString('en-GB')],
    ['Claims cited', String(claims)],
    ['Written by', script.llm_model],
    ['Generated', formatDateTime(script.created_at)],
  ];
  return (
    <Card>
      <CardHeader
        title="Script details"
        icon={<FileText className="size-4" aria-hidden />}
        as="h2"
      />
      <CardBody>
        <dl className="divide-y divide-line">
          {rows.map(([term, value]) => (
            <div
              key={term}
              className="flex items-start justify-between gap-4 py-2.5 first:pt-0 last:pb-0"
            >
              <dt className="text-sm text-ink-500">{term}</dt>
              <dd className="text-right text-sm font-semibold text-ink-950 break-anywhere">
                {value}
              </dd>
            </div>
          ))}
        </dl>
      </CardBody>
    </Card>
  );
}

// --- the workspace -------------------------------------------------------------------------

/** A three-column workspace: sections on the left, the script in the middle, settings and approval on the right. */
export function ScriptStudio({ project, script, topics }: ScriptStudioProps) {
  const [selected, setSelected] = useState(0);
  const index = Math.min(selected, Math.max(0, script.segments.length - 1));
  const { data: synthesisStatus } = useSynthesisStatus(
    project.id,
    script.status === 'approved' ? script.id : undefined,
  );

  if (script.segments.length === 0) {
    return (
      <Alert tone="warning" title="This script has no sections">
        Something went wrong while it was written. Please write a new version.
      </Alert>
    );
  }

  const currentSegment = script.segments[index];
  const currentSegmentAudio = currentSegment
    ? synthesisStatus?.segments.find((entry) => entry.id === currentSegment.id)?.audio
    : undefined;

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[220px_minmax(0,1fr)] xl:grid-cols-[240px_minmax(0,1fr)_320px]">
      <div className="min-w-0 lg:sticky lg:top-24 lg:self-start">
        <SectionNav script={script} topics={topics} selected={index} onSelect={setSelected} />
      </div>
      <div className="min-w-0 space-y-6">
        <SectionContent
          script={script}
          topics={topics}
          selected={index}
          onSelect={setSelected}
          projectId={project.id}
          segmentAudio={currentSegmentAudio ?? undefined}
        />
      </div>
      <aside
        aria-label="Project settings and approval"
        className="space-y-6 lg:col-span-2 lg:grid lg:grid-cols-3 lg:gap-6 lg:space-y-0 xl:col-span-1 xl:block xl:space-y-6"
      >
        <ApprovalCard project={project} script={script} />
        <SynthesisCard project={project} script={script} />
        <ProjectSettingsCard project={project} script={script} />
        <DetailsCard script={script} />
      </aside>
    </div>
  );
}
