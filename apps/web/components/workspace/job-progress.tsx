'use client';

import { Check, Circle, Info, Loader2, RefreshCw } from 'lucide-react';
import type { ReactNode } from 'react';
import type { Job } from '@edutoon/contracts';
import { Button } from '@/components/ui/button';
import { Card, CardBody } from '@/components/ui/card';
import { Alert, Progress } from '@/components/ui/feedback';
import { cn } from '@/lib/cn';
import { formatElapsed } from '@/lib/format';
import { assessWait } from '@/lib/jobs';
import { useNow } from '@/lib/use-now';

export type ProgressStepState = 'done' | 'active' | 'pending';

export interface ProgressStep {
  label: string;
  state: ProgressStepState;
}

/** The steps every background job goes through, from what the API really knows: its status. */
export function stepsForJob(job: Job, labels: { ready: string; working: string }): ProgressStep[] {
  return [
    { label: labels.ready, state: 'done' },
    { label: 'Waiting for its turn', state: job.status === 'queued' ? 'active' : 'done' },
    { label: labels.working, state: job.status === 'running' ? 'active' : 'pending' },
  ];
}

function StepMarker({ state }: { state: ProgressStepState }) {
  if (state === 'done')
    return (
      <span className="flex size-6 items-center justify-center rounded-full bg-success-600 text-white">
        <Check className="size-3.5" strokeWidth={3} aria-hidden />
      </span>
    );
  if (state === 'active')
    return <Loader2 className="size-6 animate-spin text-brand-600" aria-hidden />;
  return <Circle className="size-6 text-ink-300" aria-hidden />;
}

const STATE_TEXT: Record<ProgressStepState, string> = {
  done: 'Done',
  active: 'In progress',
  pending: 'Waiting',
};

interface JobProgressCardProps {
  icon: ReactNode;
  title: string;
  description: string;
  job: Job;
  steps: ProgressStep[];
  onRefresh?: () => void;
  refreshing?: boolean;
  /** Extra notes (for example, that editing is paused while this runs). */
  footer?: ReactNode;
}

/**
 * Progress for one background job. It shows what is really known - queued or running, and for
 * how long - and says so plainly when a job is taking longer than usual. It never invents a
 * percentage, so it can never sit at "99%" for ever.
 */
export function JobProgressCard({
  icon,
  title,
  description,
  job,
  steps,
  onRefresh,
  refreshing,
  footer,
}: JobProgressCardProps) {
  const now = useNow(1000);
  const wait = assessWait(job, now);

  return (
    <Card>
      <CardBody className="space-y-6 py-8 sm:px-10">
        <div className="flex items-start gap-4">
          <span className="flex size-14 shrink-0 items-center justify-center rounded-full bg-brand-50 text-brand-600">
            {icon}
          </span>
          <div className="min-w-0">
            <h2 className="text-xl font-semibold text-ink-950">{title}</h2>
            <p className="mt-1 text-sm text-ink-500">{description}</p>
          </div>
        </div>

        <Progress label={title} />

        <ol className="space-y-3" aria-live="polite">
          {steps.map((step) => (
            <li key={step.label} className="flex items-center gap-3">
              <StepMarker state={step.state} />
              <span
                className={cn(
                  'text-sm',
                  step.state === 'pending' ? 'text-ink-400' : 'font-medium text-ink-950',
                )}
              >
                {step.label}
              </span>
              <span className="ml-auto text-[13px] text-ink-500">{STATE_TEXT[step.state]}</span>
            </li>
          ))}
        </ol>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4 text-[13px] text-ink-500">
          <span>
            {job.status === 'queued' ? 'Waiting for' : 'Running for'}{' '}
            <span className="font-semibold text-ink-700 tabular-nums">
              {formatElapsed(wait.elapsedMs)}
            </span>
          </span>
          {onRefresh ? (
            <Button
              size="sm"
              variant="ghost"
              onClick={onRefresh}
              loading={refreshing}
              leadingIcon={<RefreshCw className="size-3.5" aria-hidden />}
            >
              Check now
            </Button>
          ) : null}
        </div>

        {wait.message ? (
          <Alert tone={wait.level === 'stalled' ? 'warning' : 'info'}>{wait.message}</Alert>
        ) : (
          <p className="flex gap-2 rounded-md bg-brand-50 p-3 text-[13px] text-ink-600">
            <Info className="mt-0.5 size-4 shrink-0 text-brand-600" aria-hidden />
            You can leave this page. We will keep going, and it will be ready when you come back.
          </p>
        )}
        {footer}
      </CardBody>
    </Card>
  );
}
