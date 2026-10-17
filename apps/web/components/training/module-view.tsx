'use client';

import { ArrowLeft, ArrowRight, Lock } from 'lucide-react';
import Link from 'next/link';
import { useEffect, useState } from 'react';
import type { Attempt, EmployeeAssessment, MyCourse, MyModule } from '@edutoon/contracts';
import { Badge } from '@/components/ui/badge';
import { Button, ButtonLink } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Alert, ErrorState, Skeleton } from '@/components/ui/feedback';
import { PageHeader } from '@/components/ui/page-header';
import { ApiError, describeError } from '@/lib/api/errors';
import { fetchModuleVideoUrl } from '@/lib/api/training';
import {
  useMyAssessment,
  useMyAttempts,
  useMyCourses,
  useSubmitAttempt,
} from '@/lib/api/training-queries';
import { formatDateTime, formatDuration } from '@/lib/format';
import { bestAttempt, scoreLabel } from '@/lib/training';

function findModule(
  courses: readonly MyCourse[],
  moduleId: string,
): { course: MyCourse; module: MyModule; next: MyModule | undefined } | undefined {
  for (const course of courses) {
    const index = course.modules.findIndex((module) => module.id === moduleId);
    if (index >= 0) {
      return { course, module: course.modules[index]!, next: course.modules[index + 1] };
    }
  }
  return undefined;
}

/** The module's video, played from a `blob:` URL fetched with the person's own token. */
function ModuleVideo({ module }: { module: MyModule }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    setUrl(null);
    setFailed(false);
    fetchModuleVideoUrl(module.id)
      .then((fetched) => {
        if (cancelled) {
          URL.revokeObjectURL(fetched);
          return;
        }
        objectUrl = fetched;
        setUrl(fetched);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [module.id]);

  if (failed) {
    return (
      <Alert tone="danger" title="This video could not be loaded">
        Try again in a moment.
      </Alert>
    );
  }
  if (!url) return <Skeleton className="aspect-video w-full" />;
  return (
    <video
      controls
      src={url}
      aria-label={`Video: ${module.title}`}
      className="aspect-video w-full rounded-lg bg-ink-950"
    />
  );
}

function AttemptHistory({ attempts }: { attempts: readonly Attempt[] }) {
  if (attempts.length === 0) return null;
  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold text-ink-950">Your attempts</h3>
      <ul className="divide-y divide-line rounded-md border border-line" aria-label="Your attempts">
        {[...attempts]
          .sort((a, b) => b.attempt_number - a.attempt_number)
          .map((attempt) => (
            <li key={attempt.id} className="flex flex-wrap items-center gap-3 px-3 py-2 text-sm">
              <span className="font-medium text-ink-900">Attempt {attempt.attempt_number}</span>
              <span className="text-ink-600">{scoreLabel(attempt)}</span>
              <span className="text-[13px] text-ink-500">
                {formatDateTime(attempt.submitted_at)}
              </span>
              <Badge tone={attempt.passed ? 'success' : 'danger'} className="ml-auto">
                {attempt.passed ? 'Passed' : 'Not passed'}
              </Badge>
            </li>
          ))}
      </ul>
    </div>
  );
}

interface QuestionsProps {
  assessment: EmployeeAssessment;
  moduleId: string;
  /** Once true the answers are fixed: they were submitted and graded. */
  locked: boolean;
  answers: Record<string, string>;
  onAnswer: (questionId: string, optionId: string) => void;
}

function Questions({ assessment, locked, answers, onAnswer }: QuestionsProps) {
  return (
    <ol className="space-y-6">
      {assessment.questions.map((question) => (
        <li key={question.id}>
          <fieldset disabled={locked}>
            <legend className="text-sm font-semibold text-ink-950">
              {question.position}. {question.prompt}
            </legend>
            <div className="mt-2 space-y-2">
              {question.options.map((option) => (
                <label
                  key={option.id}
                  className={`flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2 text-sm ${
                    answers[question.id] === option.id
                      ? 'border-brand-500 bg-brand-50'
                      : 'border-line hover:border-line-strong'
                  } ${locked ? 'cursor-not-allowed opacity-80' : ''}`}
                >
                  <input
                    type="radio"
                    name={`q-${question.id}`}
                    value={option.id}
                    checked={answers[question.id] === option.id}
                    onChange={() => onAnswer(question.id, option.id)}
                    className="mt-0.5"
                  />
                  <span>{option.label}</span>
                </label>
              ))}
            </div>
          </fieldset>
        </li>
      ))}
    </ol>
  );
}

function AssessmentPanel({ module, next }: { module: MyModule; next: MyModule | undefined }) {
  const assessment = useMyAssessment(module.id, true);
  const attempts = useMyAttempts(module.id, true);
  const submit = useSubmitAttempt(module.id);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [result, setResult] = useState<Attempt | null>(null);

  const noAssessment =
    assessment.error instanceof ApiError && assessment.error.kind === 'not_found';

  if (assessment.isPending || attempts.isPending) return <Skeleton className="h-48 w-full" />;
  if (noAssessment) {
    return (
      <Card>
        <CardHeader title="Assessment" />
        <CardBody>
          <p className="text-sm text-ink-600">
            This module has no assessment. {next ? 'The next module is open to you.' : ''}
          </p>
        </CardBody>
      </Card>
    );
  }
  if (assessment.isError) {
    return (
      <ErrorState
        error={assessment.error}
        action="load the assessment"
        onRetry={() => void assessment.refetch()}
        retrying={assessment.isRefetching}
      />
    );
  }

  const data = assessment.data;
  const history = attempts.data ?? [];
  const summary = bestAttempt(history);
  const answered = Object.keys(answers).length;
  const complete = answered === data.question_count;
  const submitError = submit.isError
    ? describeError(submit.error, { action: 'submit your answers' })
    : null;

  const send = () => {
    submit.mutate(
      {
        answers: data.questions.map((question) => ({
          question_id: question.id,
          option_id: answers[question.id]!,
        })),
      },
      { onSuccess: (attempt) => setResult(attempt) },
    );
  };

  const tryAgain = () => {
    setAnswers({});
    setResult(null);
    submit.reset();
  };

  return (
    <Card>
      <CardHeader
        title="Assessment"
        description={`${data.question_count} ${data.question_count === 1 ? 'question' : 'questions'} · pass mark ${data.threshold_percent}%`}
        action={
          summary.passed ? (
            <Badge tone="success" dot>
              Passed
            </Badge>
          ) : undefined
        }
      />
      <CardBody className="space-y-6">
        {summary.passed && !result ? (
          <Alert tone="success" title="You have passed this module">
            Your best result was {scoreLabel(summary.best)}.{' '}
            {next ? 'The next module is open to you.' : 'That is the last module of the course.'}
          </Alert>
        ) : (
          <Questions
            assessment={data}
            moduleId={module.id}
            locked={result !== null || submit.isPending}
            answers={answers}
            onAnswer={(questionId, optionId) =>
              setAnswers((current) => ({ ...current, [questionId]: optionId }))
            }
          />
        )}

        {result ? (
          <Alert
            tone={result.passed ? 'success' : 'warning'}
            title={result.passed ? 'You passed' : 'Not passed this time'}
          >
            You answered {scoreLabel(result)}; the pass mark is {result.threshold_percent}%. Your
            answers are submitted and cannot be changed.
          </Alert>
        ) : null}
        {submitError ? (
          <Alert tone="danger" title={submitError.title}>
            {submitError.message}
          </Alert>
        ) : null}

        <div className="flex flex-wrap items-center gap-3">
          {!summary.passed && !result ? (
            <Button onClick={send} loading={submit.isPending} disabled={!complete}>
              Submit answers
            </Button>
          ) : null}
          {!summary.passed && !result && !complete ? (
            <span className="text-[13px] text-ink-500">
              Answer every question to submit ({answered} of {data.question_count} answered).
            </span>
          ) : null}
          {result && !result.passed ? <Button onClick={tryAgain}>Try again</Button> : null}
          {(result?.passed || summary.passed) && next ? (
            <ButtonLink
              href={`/learn/${next.id}`}
              trailingIcon={<ArrowRight className="size-4" aria-hidden />}
            >
              Next module: {next.title}
            </ButtonLink>
          ) : null}
        </div>

        <AttemptHistory attempts={history} />
      </CardBody>
    </Card>
  );
}

/** One module: its video, then its assessment. A locked module says what to finish first. */
export function ModuleView({ moduleId }: { moduleId: string }) {
  const courses = useMyCourses();

  if (courses.isPending) return <Skeleton className="h-96 w-full" />;
  if (courses.isError) {
    return (
      <ErrorState
        error={courses.error}
        action="load this module"
        onRetry={() => void courses.refetch()}
        retrying={courses.isRefetching}
      />
    );
  }

  const found = findModule(courses.data, moduleId);
  if (!found) {
    return (
      <Alert tone="warning" title="This module is not available to you">
        It may not be published, or you may not be enrolled in its course.{' '}
        <Link href="/learn" className="font-semibold text-brand-700 underline">
          Back to your courses
        </Link>
      </Alert>
    );
  }
  const { course, module, next } = found;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={
          <Link href="/learn" className="inline-flex items-center gap-1 hover:underline">
            <ArrowLeft className="size-4" aria-hidden /> {course.title}
          </Link>
        }
        title={`${module.position}. ${module.title}`}
        description={`${formatDuration(module.duration_ms)}${module.description ? ` · ${module.description}` : ''}`}
      />
      {module.locked && module.lock ? (
        <Alert tone="warning" title="This module is locked">
          <span className="inline-flex items-center gap-2">
            <Lock className="size-4" aria-hidden /> Pass &ldquo;{module.lock.blocking_module_title}
            &rdquo; (module {module.lock.blocking_module_position}) first.
          </span>
          <div className="mt-3">
            <ButtonLink href={`/learn/${module.lock.blocking_module_id}`} variant="secondary">
              Go to module {module.lock.blocking_module_position}
            </ButtonLink>
          </div>
        </Alert>
      ) : (
        <>
          <ModuleVideo module={module} />
          <AssessmentPanel module={module} next={next} />
        </>
      )}
    </div>
  );
}
