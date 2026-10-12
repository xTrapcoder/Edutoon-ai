import { AlertCircle, Check, CircleHelp, Loader2, Lock } from 'lucide-react';
import Link from 'next/link';
import { cn } from '@/lib/cn';
import { PIPELINE_STATUS, type PipelineStage, type PipelineStageId } from '@/lib/pipeline';
import type { Tone } from '@/lib/workflow';

interface PipelineStepperProps {
  stages: readonly PipelineStage[];
  /** The stage whose page is on screen. */
  viewing?: PipelineStageId;
  className?: string;
}

const STATUS_TEXT: Record<Tone, string> = {
  neutral: 'text-ink-500',
  info: 'text-brand-700',
  brand: 'text-accent-700',
  success: 'text-success-700',
  warning: 'text-warning-700',
  danger: 'text-danger-700',
};

function Marker({ stage, index }: { stage: PipelineStage; index: number }) {
  const { status } = stage;
  return (
    <span
      aria-hidden
      className={cn(
        'relative z-10 flex size-8 items-center justify-center rounded-full border-2 text-sm font-semibold transition-colors',
        status === 'completed' && 'border-brand-600 bg-brand-600 text-white',
        status === 'processing' && 'border-brand-600 bg-brand-50 text-brand-700',
        status === 'failed' && 'border-danger-600 bg-danger-50 text-danger-700',
        status === 'unknown' && 'border-warning-600 bg-warning-50 text-warning-700',
        (status === 'pending' || status === 'blocked' || status === 'checking') &&
          'border-line-strong bg-surface text-ink-400',
      )}
    >
      {status === 'completed' ? (
        <Check className="size-4" strokeWidth={3} />
      ) : status === 'processing' ? (
        <Loader2 className="size-4 animate-spin" />
      ) : status === 'failed' ? (
        <AlertCircle className="size-4" />
      ) : status === 'blocked' ? (
        <Lock className="size-3.5" />
      ) : status === 'unknown' ? (
        <CircleHelp className="size-4" />
      ) : (
        index + 1
      )}
    </span>
  );
}

/**
 * Source → Settings → Topics → Claims → Script → Narration → Storyboard → Video. Every stage is a
 * link to its own page and carries the status the API really reports for it; a stage that is waiting
 * on an earlier one says "Blocked" and its page says what for.
 */
export function PipelineStepper({ stages, viewing, className }: PipelineStepperProps) {
  return (
    <ol
      aria-label="Project progress"
      className={cn('grid grid-cols-4 gap-y-5 lg:grid-cols-8', className)}
    >
      {stages.map((stage, index) => {
        const status = PIPELINE_STATUS[stage.status];
        const reason = stage.blocker ?? stage.detail;
        // Four to a row on narrow screens: no connector into the first stage of a row.
        const connector = index === 0 ? 'hidden' : index % 4 === 0 ? 'hidden lg:block' : 'block';
        const previous = stages[index - 1];

        return (
          <li key={stage.id} className="relative min-w-0 px-1">
            {previous ? (
              <span
                aria-hidden
                className={cn(
                  'absolute top-4 h-0.5 -translate-y-1/2',
                  connector,
                  stage.status === 'completed' || previous.status === 'completed'
                    ? 'bg-brand-600'
                    : 'bg-line-strong',
                )}
                style={{ left: 'calc(-50% + 16px)', right: 'calc(50% + 16px)' }}
              />
            ) : null}
            <Link
              href={stage.href}
              aria-current={viewing === stage.id ? 'page' : undefined}
              title={reason ? `${status.long}: ${reason}` : status.long}
              className="block rounded-md py-1 transition-opacity hover:opacity-80"
            >
              <span className="flex flex-col items-center gap-1.5 text-center">
                <Marker stage={stage} index={index} />
                <span className="block max-w-full">
                  <span
                    className={cn(
                      'block truncate text-[13px] font-semibold sm:text-sm',
                      viewing === stage.id
                        ? 'text-brand-800 underline underline-offset-4'
                        : 'text-ink-950',
                    )}
                  >
                    {stage.label}
                  </span>
                  <span
                    className={cn('block truncate text-xs font-semibold', STATUS_TEXT[status.tone])}
                  >
                    {status.label}
                  </span>
                  {reason ? (
                    <span className="hidden max-w-full text-xs text-ink-500 md:line-clamp-2">
                      {reason}
                    </span>
                  ) : null}
                </span>
                <span className="sr-only">
                  ({status.long}
                  {reason ? `: ${reason}` : ''})
                </span>
              </span>
            </Link>
          </li>
        );
      })}
    </ol>
  );
}
