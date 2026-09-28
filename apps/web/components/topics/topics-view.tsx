'use client';

import { ArrowRight, FilePlus2, ListTree, Plus, RefreshCw, Sparkles, Wand2 } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Button, ButtonLink } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Alert } from '@/components/ui/feedback';
import { Modal } from '@/components/ui/modal';
import { describeError } from '@/lib/api/errors';
import { useStartExtraction } from '@/lib/api/queries';
import { claimsForTopic } from '@/lib/claims';
import { pluralise } from '@/lib/format';
import { isActiveJob, jobFailureMessage, latestJobOfKind, readExtractionSummary } from '@/lib/jobs';
import { AddKeywordsForm, AddPdfForm } from '@/components/workspace/add-source';
import { JobProgressCard, stepsForJob } from '@/components/workspace/job-progress';
import { QueryGate } from '@/components/workspace/query-gate';
import { useWorkspaceContext } from '@/components/workspace/workspace-context';
import { ClaimExtractionPanel } from './claim-extraction-panel';
import { TopicDetail } from './topic-detail';
import { TopicList } from './topic-list';
import { buildTree, countTopics, findTopic } from './topic-tree';

function ExtractButton({
  replace,
  onStart,
  loading,
  disabled,
  children,
}: {
  replace: boolean;
  onStart: (replace: boolean) => void;
  loading: boolean;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <Button
      onClick={() => onStart(replace)}
      loading={loading}
      disabled={disabled}
      size="lg"
      leadingIcon={<Wand2 className="size-5" aria-hidden />}
    >
      {children}
    </Button>
  );
}

function TopicsView() {
  const { project, workflow, sources, topics, jobs, claims } = useWorkspaceContext();
  const start = useStartExtraction(project.id);
  const topicList = useMemo(() => topics.data ?? [], [topics.data]);
  const claimList = useMemo(() => claims.data ?? [], [claims.data]);

  const extraction = latestJobOfKind(jobs.data, 'extract_topics');
  const active = extraction !== undefined && isActiveJob(extraction);
  const derived = topicList.some((topic) => topic.origin !== 'user');
  const hasTopics = topicList.length > 0;
  const summary = readExtractionSummary(extraction);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const [confirmingRegenerate, setConfirmingRegenerate] = useState(false);
  const [addingKeywords, setAddingKeywords] = useState(false);
  const detailRef = useRef<HTMLDivElement>(null);

  const selected = findTopic(topicList, selectedId);
  const counts = countTopics(topicList);

  // When extraction finishes for the first time, land on the first topic.
  useEffect(() => {
    if (selectedId === null && topicList[0]) setSelectedId(topicList[0].id);
  }, [selectedId, topicList]);

  const select = (id: string) => {
    setSelectedId(id);
    // On a narrow screen the detail sits below the list: bring it into view.
    if (window.matchMedia('(max-width: 1023px)').matches) {
      detailRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  };

  const toggle = (id: string) =>
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const parents = useMemo(
    () =>
      buildTree(topicList).flatMap(function collect(node): string[] {
        return node.children.length > 0 ? [node.topic.id, ...node.children.flatMap(collect)] : [];
      }),
    [topicList],
  );

  const begin = (replace: boolean) => {
    setConfirmingRegenerate(false);
    start.mutate(
      { replace },
      {
        onSuccess: () => toast.success('Extraction started'),
      },
    );
  };

  const startError = start.isError
    ? describeError(start.error, { action: 'start topic extraction' })
    : null;
  const failed = extraction?.status === 'failed';

  // --- the source is not ready ---------------------------------------------------------
  const sourceMissing = workflow.stage === 'needs_source' || workflow.stage === 'source_failed';
  if (sourceMissing && !hasTopics) {
    return (
      <Card>
        <CardHeader
          title={project.source_type === 'pdf' ? 'Add your PDF first' : 'Add your keywords first'}
          description="Topics are found from your source, so we need that before we can start."
          icon={<FilePlus2 className="size-4" aria-hidden />}
        />
        <CardBody>
          {project.source_type === 'pdf' ? (
            <AddPdfForm projectId={project.id} />
          ) : (
            <AddKeywordsForm projectId={project.id} />
          )}
        </CardBody>
      </Card>
    );
  }

  // --- extraction is running ------------------------------------------------------------
  const progress =
    active && extraction ? (
      <JobProgressCard
        icon={<ListTree className="size-7" aria-hidden />}
        title="Extracting topics from your content"
        description="We are working out what is worth covering, and which pages each topic comes from."
        job={extraction}
        steps={stepsForJob(extraction, {
          ready:
            project.source_type === 'pdf' && sources.data?.length
              ? 'Your PDF has been read'
              : 'Your keywords are saved',
          working: 'Extracting and organising topics',
        })}
        onRefresh={() => void jobs.refetch()}
        refreshing={jobs.isRefetching}
        footer={
          hasTopics ? (
            <p className="text-[13px] text-ink-500">
              Your current topics are shown below. Their wording is locked until this finishes.
            </p>
          ) : null
        }
      />
    ) : null;

  if (!hasTopics) {
    return (
      <div className="space-y-6">
        {progress}
        {!active ? (
          <Card>
            <CardBody className="flex flex-col items-center gap-5 py-12 text-center">
              <span className="flex size-14 items-center justify-center rounded-full bg-brand-50 text-brand-600">
                <Sparkles className="size-7" aria-hidden />
              </span>
              <div className="max-w-lg">
                <h2 className="text-xl font-semibold text-ink-950">
                  {failed ? "Let's try that again" : 'Ready to find your topics'}
                </h2>
                <p className="mt-2 text-sm text-ink-500">
                  We will read your material and outline what is worth covering, with the pages each
                  topic comes from. Nothing is added to a script until you have reviewed it.
                </p>
              </div>
              {failed ? (
                <Alert
                  tone="danger"
                  title="The last attempt didn't finish"
                  className="w-full max-w-lg text-left"
                >
                  {jobFailureMessage(extraction)}
                </Alert>
              ) : null}
              {startError ? (
                <Alert tone="danger" title={startError.title} className="w-full max-w-lg text-left">
                  {startError.message}
                </Alert>
              ) : null}
              <ExtractButton replace={false} onStart={begin} loading={start.isPending}>
                {failed ? 'Try again' : 'Extract topics'}
              </ExtractButton>
            </CardBody>
          </Card>
        ) : null}
      </div>
    );
  }

  // --- review ----------------------------------------------------------------------------
  const scriptExists = workflow.script !== undefined;
  const canProceed = workflow.usableTopicCount > 0 && !active;
  const regenerateBlocked = scriptExists;

  return (
    <div className="space-y-6">
      {progress}

      {failed && !active ? (
        <Alert
          tone="danger"
          title="The last extraction didn't finish"
          action={
            <Button
              size="sm"
              variant="secondary"
              onClick={() => begin(derived)}
              loading={start.isPending}
            >
              Try again
            </Button>
          }
        >
          {jobFailureMessage(extraction)} Your topics are exactly as they were.
        </Alert>
      ) : null}
      {summary?.truncated && !active ? (
        <Alert tone="info" title="Topics were drawn from a sample of a long document">
          Your document is long, so topics were found from an even spread of pages across it rather
          than every page.
        </Alert>
      ) : null}
      {startError ? (
        <Alert tone="danger" title={startError.title}>
          {startError.message}
        </Alert>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold text-ink-950">Review your topics</h2>
          <p className="mt-1 text-sm text-ink-500" aria-live="polite">
            {pluralise(counts.total, 'topic')} · {counts.accepted} accepted · {counts.toReview} to
            review · {counts.rejected} rejected
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button
            variant="secondary"
            onClick={() => setAddingKeywords((value) => !value)}
            aria-expanded={addingKeywords}
            leadingIcon={<Plus className="size-4" aria-hidden />}
          >
            Add keywords
          </Button>
          {derived ? (
            <Button
              variant="secondary"
              onClick={() => setConfirmingRegenerate(true)}
              disabled={active || regenerateBlocked}
              title={
                regenerateBlocked
                  ? 'A script already uses these topics, so they cannot be replaced'
                  : undefined
              }
              leadingIcon={<RefreshCw className="size-4" aria-hidden />}
            >
              Regenerate topics
            </Button>
          ) : (
            <Button
              variant="secondary"
              onClick={() => begin(false)}
              loading={start.isPending}
              disabled={active}
              leadingIcon={<Wand2 className="size-4" aria-hidden />}
            >
              {project.source_type === 'pdf' && sources.data?.length
                ? 'Extract topics from PDF'
                : 'Suggest related topics'}
            </Button>
          )}
          {canProceed ? (
            <ButtonLink
              href={`/projects/${project.id}/script`}
              trailingIcon={<ArrowRight className="size-4" aria-hidden />}
            >
              {scriptExists ? 'Go to script' : 'Proceed to script'}
            </ButtonLink>
          ) : (
            <Button
              disabled
              trailingIcon={<ArrowRight className="size-4" aria-hidden />}
              title={
                active ? 'Wait for extraction to finish' : 'Keep at least one topic to continue'
              }
            >
              Proceed to script
            </Button>
          )}
        </div>
      </div>

      {addingKeywords ? (
        <Card>
          <CardHeader
            title="Add your own topics"
            description="Each keyword becomes a topic of yours, kept exactly as you write it."
          />
          <CardBody>
            <AddKeywordsForm
              projectId={project.id}
              disabled={active}
              disabledReason="Keywords can be added once the current extraction has finished."
            />
          </CardBody>
        </Card>
      ) : null}

      <ClaimExtractionPanel
        projectId={project.id}
        topics={topicList}
        claims={claimList}
        jobs={jobs.data ?? []}
        onRefreshJobs={() => void jobs.refetch()}
        refreshingJobs={jobs.isRefetching}
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)]">
        <Card className="lg:sticky lg:top-24 lg:max-h-[calc(100vh-8rem)] lg:self-start lg:overflow-hidden">
          <CardHeader
            title="Topic hierarchy"
            as="h3"
            action={
              parents.length > 0 ? (
                <button
                  type="button"
                  onClick={() => setCollapsed(collapsed.size === 0 ? new Set(parents) : new Set())}
                  className="text-[13px] font-semibold text-brand-700 hover:underline"
                >
                  {collapsed.size === 0 ? 'Collapse all' : 'Expand all'}
                </button>
              ) : null
            }
          />
          <div className="max-h-[70vh] overflow-y-auto px-3 py-4 lg:max-h-[calc(100vh-12rem)]">
            <TopicList
              topics={topicList}
              selectedId={selected?.id ?? null}
              onSelect={select}
              collapsed={collapsed}
              onToggle={toggle}
            />
          </div>
        </Card>
        <div ref={detailRef} className="scroll-mt-24">
          {selected ? (
            <TopicDetail
              key={selected.id}
              projectId={project.id}
              topic={selected}
              wordingLocked={active}
              claims={claimsForTopic(claimList, selected.id)}
            />
          ) : null}
        </div>
      </div>

      <Modal
        open={confirmingRegenerate}
        onClose={() => setConfirmingRegenerate(false)}
        size="sm"
        title="Regenerate topics?"
        description="We will extract the topics again from your source."
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirmingRegenerate(false)}>
              Keep current topics
            </Button>
            <Button onClick={() => begin(true)} loading={start.isPending}>
              Regenerate
            </Button>
          </>
        }
      >
        <ul className="list-disc space-y-1.5 pl-5 text-sm text-ink-600">
          <li>
            The topics we found before are replaced, including your accept and reject choices on
            them.
          </li>
          <li>The topics you added yourself are kept, exactly as you wrote them.</li>
          <li>If it does not finish, your current topics stay as they are.</li>
        </ul>
      </Modal>
    </div>
  );
}

export function TopicsPage() {
  const { sources, topics, jobs, scripts, claims } = useWorkspaceContext();
  return (
    <QueryGate
      queries={[sources, topics, jobs, scripts, claims]}
      action="load your topics"
      label="Loading topics"
    >
      <TopicsView />
    </QueryGate>
  );
}
