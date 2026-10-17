'use client';

import { CircleAlert, ShieldCheck } from 'lucide-react';
import { useMemo } from 'react';
import { toast } from 'sonner';
import type { ClaimVerification, Job, Topic } from '@edutoon/contracts';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Alert } from '@/components/ui/feedback';
import { JobProgressCard, stepsForJob } from '@/components/workspace/job-progress';
import { describeError } from '@/lib/api/errors';
import { useStartClaimExtraction } from '@/lib/api/queries';
import { CLAIM_STATUS_META, summariseClaimStatuses } from '@/lib/claims';
import {
  isActiveJob,
  jobFailureMessage,
  latestJobOfKind,
  readClaimExtractionSummary,
} from '@/lib/jobs';
import { useWorkspaceContext } from '@/components/workspace/workspace-context';
import { eligibleAcceptedTopics } from './topic-tree';

function statusLabel(status: string): string {
  return CLAIM_STATUS_META[status as keyof typeof CLAIM_STATUS_META]?.label ?? status;
}

/** What one finished run found, in a sentence a person can act on. */
function ExtractionOutcome({
  summary,
}: {
  summary: NonNullable<ReturnType<typeof readClaimExtractionSummary>>;
}) {
  const breakdown = Object.entries(summary.claimsByStatus)
    .filter(([, count]) => count > 0)
    .map(([status, count]) => `${count} ${statusLabel(status).toLowerCase()}`)
    .join(', ');

  if ((summary.topicsProcessed ?? 0) === 0 && (summary.topicsFailed ?? 0) === 0) {
    return (
      <Alert tone="info" title="Nothing new to extract">
        Every accepted topic already has verified claims. Accept another topic and run this again to
        cover it.
      </Alert>
    );
  }

  return (
    <Alert
      tone={summary.topicsFailed ? 'warning' : 'success'}
      title={
        summary.topicsFailed
          ? `Finished, but ${summary.topicsFailed} ${summary.topicsFailed === 1 ? 'topic' : 'topics'} could not be processed`
          : 'Claim extraction finished'
      }
    >
      <p>
        Checked {summary.topicsProcessed ?? 0}{' '}
        {(summary.topicsProcessed ?? 0) === 1 ? 'topic' : 'topics'}, generating{' '}
        {summary.candidatesGenerated ?? 0}{' '}
        {(summary.candidatesGenerated ?? 0) === 1 ? 'candidate claim' : 'candidate claims'} and
        verifying {summary.claimsVerified ?? 0}
        {breakdown ? ` (${breakdown})` : ''}.
      </p>
      {summary.failures.length > 0 ? (
        <ul className="mt-2 list-disc space-y-1 pl-5">
          {summary.failures.map((failure, index) => (
            <li key={failure.topicId ?? index}>
              {failure.topicTitle ? <strong>{failure.topicTitle}:</strong> : null} {failure.error}
            </li>
          ))}
        </ul>
      ) : null}
    </Alert>
  );
}

interface ClaimExtractionPanelProps {
  projectId: string;
  topics: readonly Topic[];
  claims: readonly ClaimVerification[];
  jobs: readonly Job[];
  onRefreshJobs: () => void;
  refreshingJobs: boolean;
}

/**
 * Turns the project's accepted topics into candidate claims and verifies each one, mirroring the
 * topic-extraction card above it: the same job-progress pattern, the same plain-language failure
 * handling, and no invented percentage or state the API cannot back up.
 */
export function ClaimExtractionPanel({
  projectId,
  topics,
  claims,
  jobs,
  onRefreshJobs,
  refreshingJobs,
}: ClaimExtractionPanelProps) {
  const start = useStartClaimExtraction(projectId);
  const { project } = useWorkspaceContext();
  const generalKnowledge = project.source_type === 'topic';
  const eligible = useMemo(() => eligibleAcceptedTopics(topics), [topics]);
  const extraction = latestJobOfKind(jobs, 'extract_claims');
  const active = extraction !== undefined && isActiveJob(extraction);
  const summary = readClaimExtractionSummary(extraction);
  const counts = summariseClaimStatuses(claims);
  const failed = extraction?.status === 'failed' && !active;

  const startError = start.isError
    ? describeError(start.error, { action: 'start claim extraction' })
    : null;

  const begin = () => {
    start.mutate(undefined, { onSuccess: () => toast.success('Verifying claims') });
  };

  if (active && extraction) {
    return (
      <JobProgressCard
        icon={<ShieldCheck className="size-7" aria-hidden />}
        title="Verifying your topics' claims"
        description="We are turning your accepted topics into candidate claims, then checking each one against your source."
        job={extraction}
        steps={stepsForJob(extraction, {
          ready: 'Accepted topics gathered',
          working: 'Extracting and verifying claims',
        })}
        onRefresh={onRefreshJobs}
        refreshing={refreshingJobs}
      />
    );
  }

  return (
    <Card>
      <CardHeader
        title={
          generalKnowledge
            ? 'Write the statements for your accepted topics'
            : 'Verify claims for your accepted topics'
        }
        description={
          generalKnowledge
            ? 'Each accepted topic becomes a handful of statements from the AI model general knowledge. There is no document, so none of them is checked, and the video says so.'
            : 'Each accepted topic becomes a handful of candidate claims, checked against your source before a script can use them.'
        }
        icon={<ShieldCheck className="size-4" aria-hidden />}
      />
      <CardBody className="space-y-4">
        {counts.total > 0 ? (
          <div className="flex flex-wrap items-center gap-2 text-sm text-ink-600">
            <span className="font-medium text-ink-950">
              {counts.total} {counts.total === 1 ? 'claim' : 'claims'} verified so far:
            </span>
            {counts.supported > 0 ? (
              <Badge tone="success">{counts.supported} supported</Badge>
            ) : null}
            {counts.partiallySupported > 0 ? (
              <Badge tone="warning">{counts.partiallySupported} partially supported</Badge>
            ) : null}
            {counts.unsupported > 0 ? (
              <Badge tone="neutral">{counts.unsupported} unsupported</Badge>
            ) : null}
            {counts.contradicted > 0 ? (
              <Badge tone="danger">{counts.contradicted} contradicted</Badge>
            ) : null}
          </div>
        ) : null}

        {failed ? (
          <Alert tone="danger" title="The last attempt didn't finish">
            {jobFailureMessage(extraction)}
          </Alert>
        ) : null}
        {summary && !failed ? <ExtractionOutcome summary={summary} /> : null}
        {startError ? (
          <Alert tone="danger" title={startError.title}>
            {startError.message}
          </Alert>
        ) : null}

        {eligible.length > 0 ? (
          <Button
            onClick={begin}
            loading={start.isPending}
            leadingIcon={<ShieldCheck className="size-4" aria-hidden />}
          >
            {failed ? 'Try again' : 'Extract & verify claims'}
          </Button>
        ) : (
          <p className="flex items-center gap-2 text-sm text-ink-500">
            <CircleAlert className="size-4 shrink-0" aria-hidden />
            Accept at least one topic to verify claims against your source.
          </p>
        )}
      </CardBody>
    </Card>
  );
}
