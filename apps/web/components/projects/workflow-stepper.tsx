import { AlertCircle, Check } from 'lucide-react';
import Link from 'next/link';
import { cn } from '@/lib/cn';
import type { Step, StepId } from '@/lib/workflow';

interface WorkflowStepperProps {
  projectId: string;
  steps: readonly Step[];
  /** The step whose page is on screen. */
  viewing?: StepId;
  className?: string;
}

const HREFS: Record<StepId, (projectId: string) => string> = {
  source: (id) => `/projects/${id}`,
  topics: (id) => `/projects/${id}/topics`,
  script: (id) => `/projects/${id}/script`,
  approved: (id) => `/projects/${id}/script`,
};

function Marker({ step, index }: { step: Step; index: number }) {
  return (
    <span
      aria-hidden
      className={cn(
        'relative z-10 flex size-8 items-center justify-center rounded-full border-2 text-sm font-semibold transition-colors',
        step.state === 'complete' && 'border-brand-600 bg-brand-600 text-white',
        step.state === 'current' &&
          !step.attention &&
          'border-brand-600 bg-brand-50 text-brand-700',
        step.state === 'current' &&
          step.attention &&
          'border-danger-600 bg-danger-50 text-danger-700',
        step.state === 'upcoming' && 'border-line-strong bg-surface text-ink-400',
      )}
    >
      {step.state === 'complete' ? (
        <Check className="size-4" strokeWidth={3} />
      ) : step.attention ? (
        <AlertCircle className="size-4" />
      ) : (
        index + 1
      )}
    </span>
  );
}

function stateText(step: Step): string {
  if (step.state === 'complete') return 'completed';
  if (step.state === 'current') return 'current step';
  return step.reachable ? 'next step' : 'not available yet';
}

/**
 * Source → Topics → Script → Approved. Only stages that can really be opened are links; a later
 * stage is shown, quietly, as not yet available.
 */
export function WorkflowStepper({ projectId, steps, viewing, className }: WorkflowStepperProps) {
  return (
    <ol aria-label="Project progress" className={cn('grid grid-cols-4', className)}>
      {steps.map((step, index) => {
        const previous = steps[index - 1];
        const content = (
          <span className="flex flex-col items-center gap-2 text-center">
            <Marker step={step} index={index} />
            <span className="block max-w-full">
              <span
                className={cn(
                  'block truncate text-sm font-semibold',
                  step.state === 'upcoming' ? 'text-ink-400' : 'text-ink-950',
                )}
              >
                {step.label}
              </span>
              {step.detail ? (
                <span className="hidden max-w-full truncate text-xs text-ink-500 md:block">
                  {step.detail}
                </span>
              ) : null}
            </span>
            <span className="sr-only">({stateText(step)})</span>
          </span>
        );

        return (
          <li key={step.id} className="relative min-w-0 px-1">
            {previous ? (
              <span
                aria-hidden
                className={cn(
                  'absolute top-4 h-0.5 -translate-y-1/2',
                  step.state === 'upcoming' ? 'bg-line-strong' : 'bg-brand-600',
                )}
                style={{ left: 'calc(-50% + 16px)', right: 'calc(50% + 16px)' }}
              />
            ) : null}
            {step.reachable ? (
              <Link
                href={HREFS[step.id](projectId)}
                aria-current={viewing === step.id ? 'page' : undefined}
                className="block rounded-md py-1 transition-opacity hover:opacity-80"
              >
                {content}
              </Link>
            ) : (
              <span aria-disabled="true" className="block py-1">
                {content}
              </span>
            )}
          </li>
        );
      })}
    </ol>
  );
}
