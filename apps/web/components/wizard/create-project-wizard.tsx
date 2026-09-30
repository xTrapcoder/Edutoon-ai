'use client';

import { ArrowLeft, ArrowRight, Check, X } from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Alert } from '@/components/ui/feedback';
import { PageHeader } from '@/components/ui/page-header';
import { describeError } from '@/lib/api/errors';
import { cn } from '@/lib/cn';
import {
  DEFAULT_DURATION_MS,
  DEFAULT_LANGUAGE,
  DEFAULT_ORIENTATION,
} from '@/lib/generation-settings';
import { CreationProgress, ReviewSummary } from './review-step';
import { SettingsStep } from './settings-step';
import { SourceStep } from './source-step';
import { useProjectCreation } from './use-project-creation';
import {
  hasErrors,
  mergeKeywords,
  settingsErrors,
  sourceErrors,
  usesKeywords,
  type WizardValues,
} from './wizard-model';

const STEPS = [
  { label: 'Source', hint: 'Upload or enter content' },
  { label: 'Settings', hint: 'Video preferences' },
  { label: 'Review', hint: 'Confirm and create' },
] as const;

const INITIAL_VALUES: WizardValues = {
  title: '',
  mode: 'pdf',
  file: null,
  keywords: [],
  orientation: DEFAULT_ORIENTATION,
  durationMs: DEFAULT_DURATION_MS,
  language: DEFAULT_LANGUAGE,
};

function StepIndicator({ current }: { current: number }) {
  return (
    <ol aria-label="Steps" className="grid grid-cols-3 gap-2">
      {STEPS.map((step, index) => {
        const state = index < current ? 'complete' : index === current ? 'current' : 'upcoming';
        return (
          <li
            key={step.label}
            aria-current={state === 'current' ? 'step' : undefined}
            className="relative flex flex-col items-center gap-2 text-center"
          >
            {index > 0 ? (
              <span
                aria-hidden
                className={cn(
                  'absolute top-4 h-0.5 -translate-y-1/2',
                  state === 'upcoming' ? 'bg-line-strong' : 'bg-brand-600',
                )}
                style={{ left: 'calc(-50% + 16px)', right: 'calc(50% + 16px)' }}
              />
            ) : null}
            <span
              aria-hidden
              className={cn(
                'relative z-10 flex size-8 items-center justify-center rounded-full border-2 text-sm font-semibold',
                state === 'complete' && 'border-brand-600 bg-brand-600 text-white',
                state === 'current' && 'border-brand-600 bg-brand-50 text-brand-700',
                state === 'upcoming' && 'border-line-strong bg-surface text-ink-400',
              )}
            >
              {state === 'complete' ? <Check className="size-4" strokeWidth={3} /> : index + 1}
            </span>
            <span>
              <span
                className={cn(
                  'block text-sm font-semibold',
                  state === 'upcoming' ? 'text-ink-400' : 'text-ink-950',
                )}
              >
                {step.label}
                <span className="sr-only">
                  {state === 'complete'
                    ? ' (completed)'
                    : state === 'current'
                      ? ' (current step)'
                      : ''}
                </span>
              </span>
              <span className="hidden text-xs text-ink-500 sm:block">{step.hint}</span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}

export function CreateProjectWizard() {
  const router = useRouter();
  const [step, setStep] = useState(0);
  const [values, setValues] = useState<WizardValues>(INITIAL_VALUES);
  const [titleTouched, setTitleTouched] = useState(false);
  const [showErrors, setShowErrors] = useState(false);
  const [keywordDraft, setKeywordDraft] = useState('');
  const creation = useProjectCreation();
  const headingRef = useRef<HTMLHeadingElement>(null);

  // Moving to a new step puts focus on its heading, so a screen reader announces where it is.
  useEffect(() => {
    headingRef.current?.focus();
  }, [step]);

  const patch = (change: Partial<WizardValues>) =>
    setValues((current) => ({ ...current, ...change }));

  // Text typed in the keyword box but not yet added still counts when the person moves on.
  const withDraft = (current: WizardValues): WizardValues => {
    if (!usesKeywords(current.mode) || keywordDraft.trim() === '') return current;
    return { ...current, keywords: mergeKeywords(current.keywords, keywordDraft).keywords };
  };

  // Once the project exists the form is fixed (the rest is retrying the steps that follow);
  // until then a failure leaves the person on Review, free to go back and change things.
  const locked = creation.projectId !== null;
  const showProgress = creation.started && (locked || creation.running);
  const errors = showErrors ? sourceErrors(withDraft(values)) : {};

  const goNext = () => {
    if (step === 0) {
      const next = withDraft(values);
      if (hasErrors(sourceErrors(next))) {
        setShowErrors(true);
        // Take the person to the first thing that needs fixing.
        requestAnimationFrame(() =>
          document.querySelector<HTMLElement>('main [aria-invalid="true"]')?.focus(),
        );
        return;
      }
      setValues(next);
      setKeywordDraft('');
      setShowErrors(false);
      setStep(1);
    } else if (step === 1) {
      if (hasErrors(settingsErrors(values))) return;
      setStep(2);
    }
  };

  const create = async () => {
    const projectId = await creation.run(values);
    if (projectId) {
      toast.success('Project created');
      router.push(`/projects/${projectId}/topics`);
    }
  };

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      <PageHeader
        title="Create new project"
        description="Three quick steps. You can change the settings later."
        actions={
          <Link
            href="/dashboard"
            className="inline-flex size-10 items-center justify-center rounded-md text-ink-500 hover:bg-surface hover:text-ink-900"
            aria-label="Cancel and go back to the dashboard"
          >
            <X className="size-5" aria-hidden />
          </Link>
        }
      />

      <StepIndicator current={step} />

      <div>
        <h2
          ref={headingRef}
          tabIndex={-1}
          className="mb-5 text-xl font-semibold text-ink-950 focus:outline-none"
        >
          {step === 0
            ? 'Add your source material'
            : step === 1
              ? 'Configure generation settings'
              : 'Review and create'}
        </h2>

        {step === 0 ? (
          <SourceStep
            values={values}
            errors={errors}
            disabled={locked}
            titleTouched={titleTouched}
            onTitleEdited={() => setTitleTouched(true)}
            onChange={patch}
            onKeywordDraftChange={setKeywordDraft}
          />
        ) : null}
        {step === 1 ? <SettingsStep values={values} onChange={patch} disabled={locked} /> : null}
        {step === 2 ? (
          showProgress ? (
            <CreationProgress
              steps={creation.steps}
              failure={
                creation.failure
                  ? { step: creation.failure.step, error: creation.failure.error }
                  : null
              }
              projectId={creation.projectId}
              onRetry={() => void create()}
              retrying={creation.running}
            />
          ) : (
            <div className="space-y-4">
              {creation.failure ? (
                <Alert
                  tone="danger"
                  title={
                    describeError(creation.failure.error, { action: 'create your project' }).title
                  }
                >
                  {describeError(creation.failure.error, { action: 'create your project' }).message}
                </Alert>
              ) : null}
              <ReviewSummary values={values} />
            </div>
          )
        ) : null}
      </div>

      {!showProgress ? (
        <div className="flex items-center justify-between gap-3 border-t border-line pt-6">
          <Button
            variant="secondary"
            onClick={() => setStep((current) => Math.max(0, current - 1))}
            disabled={step === 0}
            leadingIcon={<ArrowLeft className="size-4" aria-hidden />}
          >
            Back
          </Button>
          {step < 2 ? (
            <Button onClick={goNext} trailingIcon={<ArrowRight className="size-4" aria-hidden />}>
              Next
            </Button>
          ) : (
            <Button onClick={() => void create()} loading={creation.running} size="lg">
              Create project
            </Button>
          )}
        </div>
      ) : null}
    </div>
  );
}
