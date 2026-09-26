import { ArrowLeft, Check, Circle, RefreshCw, ShieldCheck } from 'lucide-react';
import { Button, ButtonLink } from '@/components/ui/button';
import { Card, CardBody } from '@/components/ui/card';

interface VerificationRequiredProps {
  projectId: string;
  onCheckAgain?: () => void;
  checking?: boolean;
}

const STEPS = [
  { label: 'Source added', detail: 'Your material is in the project.', done: true },
  { label: 'Topics ready', detail: 'You have an outline to work from.', done: true },
  {
    label: 'Claims verified against your source',
    detail: 'Each statement is checked before it can be used.',
    done: false,
  },
  {
    label: 'Script generated',
    detail: 'Written only from the claims that passed.',
    done: false,
  },
] as const;

/**
 * Shown when the API cannot write a script yet because the project has no verified claims. It
 * says so plainly and offers nothing that would pretend otherwise.
 */
export function VerificationRequired({
  projectId,
  onCheckAgain,
  checking,
}: VerificationRequiredProps) {
  return (
    <Card>
      <CardBody className="grid grid-cols-1 gap-8 py-10 sm:px-10 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)] lg:items-center">
        <div>
          <span className="flex size-14 items-center justify-center rounded-full bg-brand-50 text-brand-600">
            <ShieldCheck className="size-7" aria-hidden />
          </span>
          <h2 className="mt-5 text-xl font-semibold text-ink-950">Verification required</h2>
          <p className="mt-2 text-[15px] leading-7 text-ink-600">
            A script is written only from claims that have been checked against your source, so that
            every statement can be traced back to where it came from.
          </p>
          <p className="mt-3 text-[15px] leading-7 text-ink-600">
            This project doesn&apos;t have any verified claims yet, so there is nothing to write
            from. Once claims have been verified you can generate the script here.
          </p>
          <div className="mt-6 flex flex-wrap gap-3">
            <ButtonLink
              href={`/projects/${projectId}/topics`}
              variant="secondary"
              leadingIcon={<ArrowLeft className="size-4" aria-hidden />}
            >
              Back to topics
            </ButtonLink>
            {onCheckAgain ? (
              <Button
                variant="ghost"
                onClick={onCheckAgain}
                loading={checking}
                leadingIcon={<RefreshCw className="size-4" aria-hidden />}
              >
                Check again
              </Button>
            ) : null}
          </div>
        </div>

        <ol
          className="space-y-1 rounded-lg border border-line bg-surface-sunken p-5"
          aria-label="Steps to a script"
        >
          {STEPS.map((step, index) => (
            <li key={step.label} className="relative flex gap-3 pb-4 last:pb-0">
              {index < STEPS.length - 1 ? (
                <span aria-hidden className="absolute top-7 bottom-0 left-3 w-px bg-line-strong" />
              ) : null}
              {step.done ? (
                <span className="relative z-10 flex size-6 shrink-0 items-center justify-center rounded-full bg-success-600 text-white">
                  <Check className="size-3.5" strokeWidth={3} aria-hidden />
                  <span className="sr-only">Done:</span>
                </span>
              ) : (
                <span className="relative z-10 flex size-6 shrink-0 items-center justify-center rounded-full bg-surface-sunken">
                  <Circle className="size-6 text-ink-300" aria-hidden />
                  <span className="sr-only">Not done yet:</span>
                </span>
              )}
              <div>
                <p
                  className={
                    step.done
                      ? 'text-sm font-semibold text-ink-950'
                      : 'text-sm font-semibold text-ink-700'
                  }
                >
                  {step.label}
                </p>
                <p className="text-[13px] text-ink-500">{step.detail}</p>
              </div>
            </li>
          ))}
        </ol>
      </CardBody>
    </Card>
  );
}
