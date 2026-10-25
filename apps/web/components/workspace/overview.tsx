'use client';

import {
  ArrowRight,
  CheckCircle2,
  FileText,
  ListChecks,
  PenLine,
  Settings2,
  Video,
} from 'lucide-react';
import { useState } from 'react';
import type { Source, Topic } from '@edutoon/contracts';
import { Badge } from '@/components/ui/badge';
import { ButtonLink, Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { ErrorState, Progress, Skeleton } from '@/components/ui/feedback';
import { formatBytes, formatDateTime, formatDuration, pluralise } from '@/lib/format';
import { languageLabel, orientationLabel } from '@/lib/generation-settings';
import { isDeckProject, isFileProject, isTextProject, sourceWords } from '@/lib/source-kind';
import { STAGES, type StageId, type Tone } from '@/lib/workflow';
import { ActivityCard } from './activity-card';
import { AddSourceForm, PasteTextForm } from './add-source';
import { DeleteProjectCard } from './delete-project';
import { EditSettingsModal } from './edit-settings-modal';
import { InstructionsCard } from './instructions-card';
import { useWorkspaceContext } from './workspace-context';

const SOURCE_STATUS: Record<Source['status'], { label: string; tone: Tone }> = {
  pending: { label: 'Waiting', tone: 'neutral' },
  processing: { label: 'Reading', tone: 'info' },
  parsed: { label: 'Ready', tone: 'success' },
  failed: { label: "Couldn't read", tone: 'danger' },
};

interface Guidance {
  title: string;
  body: string;
}

/** What differs for a PowerPoint deck: the wording about the file and its slides. */
const DECK_GUIDANCE: Partial<Record<StageId, Guidance>> = {
  needs_source: {
    title: 'Add your PowerPoint deck',
    body: 'The lesson is written from your slides - their titles, bullets, tables and speaker notes - and nothing else.',
  },
  source_failed: {
    title: "We couldn't read that deck",
    body: 'It may be damaged, protected, an older .ppt file, or slides that are only pictures. Save it as .pptx and try again.',
  },
  reading_source: {
    title: 'Reading your deck',
    body: 'This only takes a moment. The next step opens as soon as it is done.',
  },
  ready_for_topics: {
    title: 'Ready to group your slides',
    body: 'We will group your slides into topics, with the slides each topic comes from.',
  },
};

const GUIDANCE: Record<StageId, Guidance> = {
  unknown: { title: 'Loading your project', body: 'One moment.' },
  needs_source: {
    title: 'Add your source material',
    body: 'Everything in the video will be traced back to what you add here.',
  },
  source_failed: {
    title: "We couldn't read that PDF",
    body: 'The reason is shown under the file below. A scanned PDF is read with text recognition on this machine; if that could not find words, rescan it at a higher quality, or try a different PDF.',
  },
  reading_source: {
    title: 'Reading your PDF',
    body: 'This only takes a moment. The next step opens as soon as it is done.',
  },
  ready_for_topics: {
    title: 'Ready to find your topics',
    body: 'We will pick out what is worth covering, with the pages each topic comes from.',
  },
  extracting_topics: {
    title: 'Extracting your topics',
    body: 'This runs in the background. You can leave this page and come back.',
  },
  topics_failed: {
    title: "Topic extraction didn't finish",
    body: 'Nothing was changed. You can review what happened and try again.',
  },
  topics_ready: {
    title: 'Review your topics',
    body: 'Accept, reject or reword each topic, then move on to the script.',
  },
  writing_script: {
    title: 'Writing your script',
    body: 'This runs in the background. You can leave this page and come back.',
  },
  script_ready: {
    title: 'Your script is ready to review',
    body: 'Read it through, check the sources behind each section, then approve it.',
  },
  script_failed: {
    title: "The script didn't finish",
    body: 'Nothing was changed. You can review what happened and try again.',
  },
  approved: {
    title: 'Script approved',
    body: 'Next: Narration → Storyboard → Video. Each has its own page, and each waits for the one before it.',
  },
};

function NextStepCard() {
  const { project, workflow, pipeline } = useWorkspaceContext();
  const { stage, next, meta } = workflow;
  const guidance = (isDeckProject(project) ? DECK_GUIDANCE[stage] : undefined) ?? GUIDANCE[stage];
  // After approval, the way on is the first of the production stages that is not finished yet.
  const production = pipeline.filter((entry) =>
    ['narration', 'storyboard', 'video'].includes(entry.id),
  );
  const continueTo = production.find((entry) => entry.status !== 'completed');
  const video = production.find((entry) => entry.id === 'video');
  const showSourceForm =
    isFileProject(project) && (stage === 'needs_source' || stage === 'source_failed');

  return (
    <Card className="overflow-hidden">
      <div className="border-b border-line bg-brand-50/50 px-6 py-3 text-xs font-semibold tracking-wide text-brand-700 uppercase">
        Next step
      </div>
      <CardBody className="space-y-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-lg font-semibold text-ink-950">
              {stage === 'approved' ? (
                <CheckCircle2 className="size-5 text-success-600" aria-hidden />
              ) : null}
              {guidance.title}
            </h2>
            <p className="mt-1 max-w-xl text-sm text-ink-500">{guidance.body}</p>
          </div>
          {next && !showSourceForm ? (
            <div className="flex flex-wrap gap-3">
              <ButtonLink
                href={next.href}
                trailingIcon={<ArrowRight className="size-4" aria-hidden />}
              >
                {next.label}
              </ButtonLink>
              {stage === 'approved' && (continueTo ?? video) ? (
                <ButtonLink
                  href={(continueTo ?? video)!.href}
                  variant="secondary"
                  leadingIcon={<Video className="size-4" aria-hidden />}
                >
                  {continueTo ? `Continue to ${continueTo.label.toLowerCase()}` : 'Watch the video'}
                </ButtonLink>
              ) : null}
            </div>
          ) : null}
        </div>
        {meta.busy ? <Progress label={guidance.title} /> : null}
        {showSourceForm ? (
          isTextProject(project) ? (
            <PasteTextForm projectId={project.id} title={project.title} />
          ) : (
            <AddSourceForm projectId={project.id} kind={isDeckProject(project) ? 'pptx' : 'pdf'} />
          )
        ) : null}
      </CardBody>
    </Card>
  );
}

function SourceCard({ sources, topics }: { sources: readonly Source[]; topics: readonly Topic[] }) {
  const { project } = useWorkspaceContext();
  const keywords = topics.filter((topic) => topic.origin === 'user');

  return (
    <Card>
      <CardHeader
        title="Source"
        description={
          isFileProject(project)
            ? 'The material this project is built from.'
            : 'The topics you entered.'
        }
        icon={<FileText className="size-4" aria-hidden />}
      />
      <CardBody className="space-y-4">
        {isFileProject(project) ? (
          sources.length === 0 ? (
            <p className="text-sm text-ink-500">
              No {sourceWords(project).noun} has been added yet.
            </p>
          ) : (
            <ul className="divide-y divide-line">
              {sources.slice(0, 3).map((source) => {
                const status = SOURCE_STATUS[source.status];
                return (
                  <li
                    key={source.id}
                    className="flex flex-wrap items-center gap-x-4 gap-y-1 py-3 first:pt-0 last:pb-0"
                  >
                    <span className="flex size-10 shrink-0 items-center justify-center rounded-md bg-danger-50 text-danger-600">
                      <FileText className="size-5" aria-hidden />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-semibold text-ink-950">
                        {source.original_filename}
                      </p>
                      <p className="text-[13px] text-ink-500">
                        {formatBytes(source.byte_size)}
                        {source.page_count
                          ? ` · ${pluralise(source.page_count, sourceWords(project).unit)}`
                          : ''}{' '}
                        · Added {formatDateTime(source.created_at)}
                      </p>
                    </div>
                    <Badge tone={status.tone} dot>
                      {status.label}
                    </Badge>
                    {source.status === 'parsed' && source.extraction_method === 'ocr' ? (
                      <p className="basis-full text-[13px] text-ink-600">
                        Read with text recognition (OCR) on this machine:{' '}
                        {pluralise(source.ocr_page_count ?? 0, 'page')} of {source.page_count ?? 0}{' '}
                        were scans. Scanned text can contain small reading errors, so check the
                        script against your document.
                      </p>
                    ) : null}
                    {source.status === 'failed' && source.failure_reason ? (
                      <p role="alert" className="basis-full text-[13px] text-danger-700">
                        {source.failure_reason}
                      </p>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )
        ) : null}
        {keywords.length > 0 ? (
          <div>
            <p className="mb-2 text-sm font-semibold text-ink-950">
              {isFileProject(project) ? 'Your keywords' : 'Keywords'}{' '}
              <span className="font-normal text-ink-500">({keywords.length})</span>
            </p>
            <ul className="flex flex-wrap gap-1.5">
              {keywords.map((topic) => (
                <li
                  key={topic.id}
                  className="rounded-full bg-brand-50 px-2.5 py-0.5 text-[13px] text-brand-800"
                >
                  {topic.title}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}

function SummaryTiles({ topics }: { topics: readonly Topic[] }) {
  const { project, workflow } = useWorkspaceContext();
  const accepted = topics.filter((topic) => topic.status === 'accepted').length;
  const rejected = topics.filter((topic) => topic.status === 'rejected').length;
  const toReview = topics.length - accepted - rejected;
  const script = workflow.script;

  const tiles = [
    {
      icon: ListChecks,
      label: 'Topics',
      value: topics.length === 0 ? 'None yet' : pluralise(topics.length, 'topic'),
      detail:
        topics.length === 0
          ? 'Extract them from your source'
          : `${accepted} accepted · ${toReview} to review · ${rejected} rejected`,
    },
    {
      icon: PenLine,
      label: 'Script',
      value: script ? STAGES[workflow.stage].label : 'Not started',
      detail: script?.estimated_duration_ms
        ? `About ${formatDuration(script.estimated_duration_ms)} of a ${formatDuration(project.target_duration_ms)} target`
        : `Aims for ${formatDuration(project.target_duration_ms)}`,
    },
  ];

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      {tiles.map((tile) => (
        <Card key={tile.label} className="p-5">
          <div className="flex items-center gap-2 text-sm font-medium text-ink-500">
            <tile.icon className="size-4 text-brand-600" aria-hidden />
            {tile.label}
          </div>
          <p className="mt-2 text-xl font-semibold text-ink-950">{tile.value}</p>
          <p className="mt-0.5 text-[13px] text-ink-500">{tile.detail}</p>
        </Card>
      ))}
    </div>
  );
}

function SettingsCard() {
  const { project } = useWorkspaceContext();
  const [editing, setEditing] = useState(false);
  const rows: [string, string][] = [
    ['Orientation', orientationLabel(project.orientation)],
    ['Target duration', formatDuration(project.target_duration_ms)],
    ['Language', languageLabel(project.language)],
  ];

  return (
    <>
      <Card id="settings" className="scroll-mt-24">
        <CardHeader
          title="Generation settings"
          icon={<Settings2 className="size-4" aria-hidden />}
          action={
            <Button size="sm" variant="secondary" onClick={() => setEditing(true)}>
              Edit
            </Button>
          }
        />
        <CardBody>
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
        </CardBody>
      </Card>
      {/* Re-created on open so it always starts from the saved values. */}
      {editing ? (
        <EditSettingsModal project={project} open onClose={() => setEditing(false)} />
      ) : null}
    </>
  );
}

export function ProjectOverview() {
  const { project, sources, topics, jobs, scripts } = useWorkspaceContext();
  const queries = [sources, topics, jobs, scripts];
  const failed = queries.filter((query) => query.isError);

  if (failed.length > 0) {
    return (
      <ErrorState
        error={failed[0]?.error}
        action="load this project's details"
        onRetry={() => failed.forEach((query) => void query.refetch())}
        retrying={failed.some((query) => query.isRefetching)}
      />
    );
  }

  if (queries.some((query) => query.isPending)) {
    return (
      <div className="space-y-6" aria-busy="true" aria-label="Loading project details">
        <Skeleton className="h-40 w-full" />
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
        </div>
      </div>
    );
  }

  const sourceList = sources.data ?? [];
  const topicList = topics.data ?? [];
  const jobList = jobs.data ?? [];

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1.9fr)_minmax(0,1fr)]">
      <div className="space-y-6">
        <NextStepCard />
        <SummaryTiles topics={topicList} />
        <SourceCard sources={sourceList} topics={topicList} />
        <ActivityCard jobs={jobList} />
      </div>
      <div className="space-y-6">
        <SettingsCard />
        <InstructionsCard />
        <DeleteProjectCard project={project} />
      </div>
    </div>
  );
}
