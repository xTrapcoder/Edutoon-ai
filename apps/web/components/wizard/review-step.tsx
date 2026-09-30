'use client';

import { AlertTriangle, Check, Circle, Loader2 } from 'lucide-react';
import { Button, ButtonLink } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Alert } from '@/components/ui/feedback';
import { describeError } from '@/lib/api/errors';
import { formatBytes, formatTargetDuration } from '@/lib/format';
import { languageLabel, orientationLabel } from '@/lib/generation-settings';
import type { CreationStep } from './use-project-creation';
import { usesKeywords, usesPdf, type WizardValues } from './wizard-model';

const MODE_LABEL = { pdf: 'PDF document', keywords: 'Keywords', both: 'PDF + Keywords' } as const;

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-1 gap-1 py-3 sm:grid-cols-[180px_1fr] sm:gap-4">
      <dt className="text-sm text-ink-500">{label}</dt>
      <dd className="min-w-0 text-sm font-medium text-ink-950 break-anywhere">{children}</dd>
    </div>
  );
}

export function ReviewSummary({ values }: { values: WizardValues }) {
  return (
    <Card>
      <CardHeader
        title="Review your project"
        description="Check these details, then create the project."
      />
      <CardBody>
        <dl className="divide-y divide-line">
          <Row label="Title">{values.title.trim()}</Row>
          <Row label="Source">{MODE_LABEL[values.mode]}</Row>
          {usesPdf(values.mode) && values.file ? (
            <Row label="PDF">
              {values.file.name}{' '}
              <span className="font-normal text-ink-500">({formatBytes(values.file.size)})</span>
            </Row>
          ) : null}
          {usesKeywords(values.mode) ? (
            <Row label="Keywords">
              <ul className="flex flex-wrap gap-1.5">
                {values.keywords.map((keyword) => (
                  <li
                    key={keyword}
                    className="rounded-full bg-brand-50 px-2.5 py-0.5 text-[13px] text-brand-800"
                  >
                    {keyword}
                  </li>
                ))}
              </ul>
            </Row>
          ) : null}
          <Row label="Orientation">{orientationLabel(values.orientation)}</Row>
          <Row label="Target duration">{formatTargetDuration(values.durationMs)}</Row>
          <Row label="Language">{languageLabel(values.language)}</Row>
        </dl>
      </CardBody>
    </Card>
  );
}

function StepIcon({ status }: { status: CreationStep['status'] }) {
  if (status === 'done')
    return (
      <span className="flex size-6 items-center justify-center rounded-full bg-success-600 text-white">
        <Check className="size-3.5" strokeWidth={3} aria-hidden />
      </span>
    );
  if (status === 'running')
    return <Loader2 className="size-6 animate-spin text-brand-600" aria-hidden />;
  if (status === 'error')
    return (
      <span className="flex size-6 items-center justify-center rounded-full bg-danger-600 text-white">
        <AlertTriangle className="size-3.5" aria-hidden />
      </span>
    );
  return <Circle className="size-6 text-ink-300" aria-hidden />;
}

const STATUS_TEXT: Record<CreationStep['status'], string> = {
  pending: 'Waiting',
  running: 'In progress',
  done: 'Done',
  error: 'Needs attention',
};

interface CreationProgressProps {
  steps: CreationStep[];
  failure: { step: string; error: unknown } | null;
  projectId: string | null;
  onRetry: () => void;
  retrying: boolean;
}

/** What is happening while the project is created, and what to do if a step fails part-way. */
export function CreationProgress({
  steps,
  failure,
  projectId,
  onRetry,
  retrying,
}: CreationProgressProps) {
  const copy = failure
    ? describeError(failure.error, { action: 'finish setting up your project' })
    : null;
  return (
    <Card>
      <CardHeader
        title={failure ? 'Your project needs a little help' : 'Setting up your project'}
        description={
          failure
            ? undefined
            : 'This usually takes a few seconds. A large PDF can take a little longer.'
        }
      />
      <CardBody className="space-y-5">
        <ol className="space-y-3" aria-live="polite">
          {steps.map((step) => (
            <li key={step.key} className="flex items-center gap-3">
              <StepIcon status={step.status} />
              <span className="text-sm font-medium text-ink-950">{step.label}</span>
              <span className="ml-auto text-[13px] text-ink-500">{STATUS_TEXT[step.status]}</span>
            </li>
          ))}
        </ol>
        {failure && copy ? (
          <Alert tone="danger" title={copy.title}>
            <p>{copy.message}</p>
            {projectId ? (
              <p className="mt-1">
                Your project was created, so nothing is lost. Try this step again, or open the
                project and add your source from there.
              </p>
            ) : null}
          </Alert>
        ) : null}
        {failure ? (
          <div className="flex flex-wrap gap-3">
            <Button onClick={onRetry} loading={retrying}>
              Try again
            </Button>
            {projectId ? (
              <ButtonLink href={`/projects/${projectId}`} variant="secondary">
                Open the project
              </ButtonLink>
            ) : null}
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}
