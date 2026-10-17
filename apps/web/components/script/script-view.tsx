'use client';

import { ArrowLeft, ListChecks, PenLine, Wand2 } from 'lucide-react';
import { useMemo } from 'react';
import { toast } from 'sonner';
import { ButtonLink, Button } from '@/components/ui/button';
import { Card, CardBody } from '@/components/ui/card';
import { Alert, EmptyState, ErrorState, Progress, Skeleton } from '@/components/ui/feedback';
import { JobProgressCard, stepsForJob } from '@/components/workspace/job-progress';
import { QueryGate } from '@/components/workspace/query-gate';
import { useWorkspaceContext } from '@/components/workspace/workspace-context';
import { ApiError, describeError } from '@/lib/api/errors';
import { useGenerateScript, useScriptDetail, useSupportedClaimCount } from '@/lib/api/queries';
import {
  isActiveJob,
  jobFailureMessage,
  latestJobOfKind,
  readScriptGenerationProgress,
  readScriptChecks,
  readWeakCitations,
  scriptGenerationStageLabel,
} from '@/lib/jobs';
import { ApprovedPanel } from './approved-panel';
import { ScriptStudio } from './script-studio';
import { VerificationRequired } from './verification-required';
import { SupportCheckNotice } from './support-check-notice';
import { WeakCitationNotice } from './weak-citation-notice';

function ReviewTopicsFirst({ projectId }: { projectId: string }) {
  return (
    <EmptyState
      icon={ListChecks}
      title="Review your topics first"
      description="A script is written after your topics are ready. Extract and review them, then come back here."
      action={
        <ButtonLink
          href={`/projects/${projectId}/topics`}
          leadingIcon={<ArrowLeft className="size-4" aria-hidden />}
        >
          Go to topics
        </ButtonLink>
      }
    />
  );
}

function GenerateScriptCard({
  onGenerate,
  loading,
  error,
  failureMessage,
}: {
  onGenerate: () => void;
  loading: boolean;
  error: ReturnType<typeof describeError> | null;
  failureMessage?: string;
}) {
  return (
    <Card>
      <CardBody className="flex flex-col items-center gap-5 py-12 text-center">
        <span className="flex size-14 items-center justify-center rounded-full bg-brand-50 text-brand-600">
          <PenLine className="size-7" aria-hidden />
        </span>
        <div className="max-w-lg">
          <h2 className="text-xl font-semibold text-ink-950">
            {failureMessage ? "Let's try that again" : 'Ready to write your script'}
          </h2>
          <p className="mt-2 text-sm text-ink-500">
            The script is written from the claims verified against your source, and you review it
            before anything else happens.
          </p>
        </div>
        {failureMessage ? (
          <Alert
            tone="danger"
            title="The last attempt didn't finish"
            className="w-full max-w-lg text-left"
          >
            {failureMessage}
          </Alert>
        ) : null}
        {error ? (
          <Alert tone="danger" title={error.title} className="w-full max-w-lg text-left">
            {error.message}
          </Alert>
        ) : null}
        <Button
          size="lg"
          onClick={onGenerate}
          loading={loading}
          leadingIcon={<Wand2 className="size-5" aria-hidden />}
        >
          {failureMessage ? 'Try again' : 'Generate script'}
        </Button>
      </CardBody>
    </Card>
  );
}

function ScriptDetailView({ scriptId }: { scriptId: string }) {
  const { project, topics, workflow, jobs } = useWorkspaceContext();
  const detail = useScriptDetail(project.id, scriptId);

  const weak = useMemo(() => readWeakCitations(jobs.data, scriptId), [jobs.data, scriptId]);
  const checks = useMemo(() => readScriptChecks(jobs.data, scriptId), [jobs.data, scriptId]);

  if (detail.isPending) {
    return (
      <div
        className="grid grid-cols-1 gap-6 lg:grid-cols-[220px_minmax(0,1fr)] xl:grid-cols-[240px_minmax(0,1fr)_320px]"
        aria-busy="true"
        aria-label="Loading script"
      >
        <Skeleton className="h-72 w-full" />
        <Skeleton className="h-96 w-full" />
        <Skeleton className="hidden h-72 w-full xl:block" />
      </div>
    );
  }
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
  return (
    <div className="space-y-6">
      {workflow.stage === 'approved' ? <ApprovedPanel projectId={project.id} /> : null}
      {checks ? <SupportCheckNotice checks={checks} /> : null}
      {weak && weak.length > 0 ? (
        <WeakCitationNotice script={detail.data} topics={topics.data ?? []} weak={weak} />
      ) : null}
      <ScriptStudio project={project} script={detail.data} topics={topics.data ?? []} />
    </div>
  );
}

function ScriptView() {
  const { project, workflow, jobs } = useWorkspaceContext();
  const claims = useSupportedClaimCount(project.id);
  const generate = useGenerateScript(project.id);
  const script = workflow.script;
  const job = useMemo(() => latestJobOfKind(jobs.data, 'generate_script'), [jobs.data]);

  if (!workflow.canOpenScript) return <ReviewTopicsFirst projectId={project.id} />;

  const start = () =>
    generate.mutate(undefined, { onSuccess: () => toast.success('Writing your script') });

  // The API answers "no verified claims" with a 400; treat it exactly like finding out up front.
  // Any other 400 (too many topics for the target length, evidence too thin for the length, ...)
  // is a different problem with its own message, which is shown as it is - never as "no claims".
  const refused =
    generate.error instanceof ApiError &&
    generate.error.kind === 'invalid' &&
    generate.error.status === 400 &&
    /no verified evidence/i.test(generate.error.serverMessage ?? '');
  const generateError =
    generate.isError && !refused
      ? describeError(generate.error, { action: 'start your script' })
      : null;

  // --- a script exists --------------------------------------------------------------------
  if (script?.status === 'draft' || script?.status === 'approved') {
    return <ScriptDetailView scriptId={script.id} />;
  }

  if (script?.status === 'pending') {
    if (job && isActiveJob(job)) {
      const progress = readScriptGenerationProgress(job);
      return (
        <JobProgressCard
          icon={<PenLine className="size-7" aria-hidden />}
          title="Writing your script"
          description="We are writing narration from the claims verified against your source."
          job={job}
          steps={stepsForJob(job, {
            ready: 'Verified claims gathered',
            working: progress ? scriptGenerationStageLabel(progress) : 'Writing the script',
          })}
          onRefresh={() => void jobs.refetch()}
          refreshing={jobs.isRefetching}
        />
      );
    }
    return (
      <Card>
        <CardBody className="space-y-4 py-10 text-center">
          <h2 className="text-xl font-semibold text-ink-950">Writing your script</h2>
          <Progress label="Writing your script" className="mx-auto max-w-sm" />
          <p className="text-sm text-ink-500">
            Getting things started. This page updates by itself.
          </p>
        </CardBody>
      </Card>
    );
  }

  // --- nothing to show yet: check that a script can be written -------------------------------
  return (
    <QueryGate
      queries={[claims]}
      action="check your verified claims"
      label="Checking your verified claims"
    >
      {claims.data === 0 || refused ? (
        <VerificationRequired
          projectId={project.id}
          onCheckAgain={() => {
            generate.reset();
            void claims.refetch();
          }}
          checking={claims.isRefetching}
        />
      ) : (
        <GenerateScriptCard
          onGenerate={start}
          loading={generate.isPending}
          error={generateError}
          failureMessage={script?.status === 'failed' ? jobFailureMessage(job) : undefined}
        />
      )}
    </QueryGate>
  );
}

export function ScriptPage() {
  const { topics, jobs, scripts } = useWorkspaceContext();
  return (
    <QueryGate queries={[topics, jobs, scripts]} action="load your script" label="Loading script">
      <ScriptView />
    </QueryGate>
  );
}
