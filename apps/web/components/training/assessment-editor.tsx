'use client';

import { Plus, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import type { Module } from '@edutoon/contracts';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Alert } from '@/components/ui/feedback';
import { Field, Input } from '@/components/ui/field';
import { Modal } from '@/components/ui/modal';
import { describeError } from '@/lib/api/errors';
import {
  useAdminAssessment,
  usePublishAssessment,
  useSaveAssessment,
} from '@/lib/api/training-queries';

interface DraftOption {
  label: string;
  correct: boolean;
}
interface DraftQuestion {
  prompt: string;
  options: DraftOption[];
}

const blankQuestion = (): DraftQuestion => ({
  prompt: '',
  options: [
    { label: '', correct: true },
    { label: '', correct: false },
  ],
});

/** Why a draft cannot be saved, or null: every question needs wording, 2+ options, one correct. */
export function draftProblem(questions: readonly DraftQuestion[]): string | null {
  if (questions.length === 0) return 'Add at least one question.';
  for (const [index, question] of questions.entries()) {
    const number = index + 1;
    if (question.prompt.trim() === '') return `Question ${number} needs wording.`;
    if (question.options.length < 2) return `Question ${number} needs at least two options.`;
    if (question.options.some((option) => option.label.trim() === ''))
      return `Every option of question ${number} needs wording.`;
    if (question.options.filter((option) => option.correct).length !== 1)
      return `Question ${number} needs exactly one correct option.`;
  }
  return null;
}

/**
 * Write or change a module's assessment: single-correct questions and the pass mark. The whole
 * assessment is saved at once; it only reaches employees once it is published. Employees never
 * receive the answer key.
 */
export function AssessmentEditor({ module, onClose }: { module: Module; onClose: () => void }) {
  const existing = useAdminAssessment(module.id, true);
  const save = useSaveAssessment(module.id);
  const publish = usePublishAssessment(module.id);
  const [threshold, setThreshold] = useState(70);
  const [questions, setQuestions] = useState<DraftQuestion[]>([blankQuestion()]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (loaded || !existing.data) return;
    setThreshold(existing.data.threshold_percent);
    setQuestions(
      existing.data.questions.map((question) => ({
        prompt: question.prompt,
        options: question.options.map((option) => ({
          label: option.label,
          correct: option.is_correct,
        })),
      })),
    );
    setLoaded(true);
  }, [existing.data, loaded]);

  const problem = draftProblem(questions);
  const status = existing.data?.status ?? null;
  const copy = save.isError
    ? describeError(save.error, { action: 'save the assessment' })
    : publish.isError
      ? describeError(publish.error, { action: 'publish the assessment' })
      : null;

  const setQuestion = (index: number, patch: Partial<DraftQuestion>) =>
    setQuestions((current) =>
      current.map((question, at) => (at === index ? { ...question, ...patch } : question)),
    );
  const setOption = (qi: number, oi: number, patch: Partial<DraftOption>) =>
    setQuestions((current) =>
      current.map((question, at) =>
        at !== qi
          ? question
          : {
              ...question,
              options: question.options.map((option, ai) =>
                ai === oi ? { ...option, ...patch } : option,
              ),
            },
      ),
    );
  const makeCorrect = (qi: number, oi: number) =>
    setQuestions((current) =>
      current.map((question, at) =>
        at !== qi
          ? question
          : {
              ...question,
              options: question.options.map((option, ai) => ({ ...option, correct: ai === oi })),
            },
      ),
    );

  const body = () => ({
    threshold_percent: threshold,
    questions: questions.map((question) => ({
      prompt: question.prompt.trim(),
      options: question.options.map((option) => ({
        label: option.label.trim(),
        is_correct: option.correct,
      })),
    })),
  });

  const busy = save.isPending || publish.isPending;

  return (
    <Modal
      open
      onClose={() => {
        if (!busy) onClose();
      }}
      dismissible={!busy}
      size="lg"
      title={`Assessment: ${module.title}`}
      description="Single-answer questions. Employees pass with the mark below, and never see which option is correct."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Close
          </Button>
          <Button
            variant="secondary"
            loading={save.isPending}
            disabled={problem !== null || busy}
            onClick={() =>
              save.mutate(body(), { onSuccess: () => toast.success('Assessment saved') })
            }
          >
            Save
          </Button>
          <Button
            loading={publish.isPending}
            disabled={problem !== null || busy}
            onClick={() => {
              // What is on screen is what gets published: save it, then publish what was saved.
              void save
                .mutateAsync(body())
                .then(() => publish.mutateAsync())
                .then(() => toast.success('Assessment published'))
                .catch(() => undefined);
            }}
          >
            {status === 'published' ? 'Save and republish' : 'Save and publish'}
          </Button>
        </>
      }
    >
      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-3">
          {status ? (
            <Badge tone={status === 'published' ? 'success' : 'neutral'} dot>
              {status === 'published' ? 'Published' : 'Draft'}
            </Badge>
          ) : (
            <Badge tone="neutral">Not saved yet</Badge>
          )}
          <Field label="Pass mark (%)" className="w-40">
            {(control) => (
              <Input
                {...control}
                type="number"
                min={1}
                max={100}
                value={threshold}
                onChange={(event) => setThreshold(Number(event.target.value))}
              />
            )}
          </Field>
        </div>
        <p className="text-[13px] text-ink-500">
          Save first, then publish. Changing a published assessment replaces it for everyone from
          then on; attempts already made keep the questions and mark they were graded with.
        </p>

        {questions.map((question, qi) => (
          <fieldset key={qi} className="space-y-3 rounded-lg border border-line p-4">
            <legend className="px-1 text-sm font-semibold text-ink-950">Question {qi + 1}</legend>
            <Field label="Question" hideLabel>
              {(control) => (
                <Input
                  {...control}
                  placeholder="The question, in words"
                  value={question.prompt}
                  onChange={(event) => setQuestion(qi, { prompt: event.target.value })}
                />
              )}
            </Field>
            {question.options.map((option, oi) => (
              <div key={oi} className="flex items-center gap-2">
                <input
                  type="radio"
                  name={`correct-${qi}`}
                  checked={option.correct}
                  onChange={() => makeCorrect(qi, oi)}
                  aria-label={`Option ${oi + 1} is correct`}
                />
                <Input
                  aria-label={`Question ${qi + 1}, option ${oi + 1}`}
                  placeholder={`Option ${oi + 1}`}
                  value={option.label}
                  onChange={(event) => setOption(qi, oi, { label: event.target.value })}
                />
                {question.options.length > 2 ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    aria-label={`Remove option ${oi + 1} of question ${qi + 1}`}
                    onClick={() =>
                      setQuestion(qi, {
                        options: question.options
                          .filter((_, ai) => ai !== oi)
                          .map((kept, ai, all) =>
                            all.some((x) => x.correct) ? kept : { ...kept, correct: ai === 0 },
                          ),
                      })
                    }
                    leadingIcon={<Trash2 className="size-4" aria-hidden />}
                  >
                    {' '}
                  </Button>
                ) : null}
              </div>
            ))}
            <div className="flex gap-2">
              {question.options.length < 6 ? (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() =>
                    setQuestion(qi, {
                      options: [...question.options, { label: '', correct: false }],
                    })
                  }
                  leadingIcon={<Plus className="size-4" aria-hidden />}
                >
                  Add option
                </Button>
              ) : null}
              {questions.length > 1 ? (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => setQuestions((current) => current.filter((_, at) => at !== qi))}
                  leadingIcon={<Trash2 className="size-4" aria-hidden />}
                >
                  Remove question
                </Button>
              ) : null}
            </div>
          </fieldset>
        ))}
        <Button
          variant="secondary"
          onClick={() => setQuestions((current) => [...current, blankQuestion()])}
          leadingIcon={<Plus className="size-4" aria-hidden />}
        >
          Add question
        </Button>

        {problem ? <p className="text-[13px] text-warning-700">{problem}</p> : null}
        {copy ? (
          <Alert tone="danger" title={copy.title}>
            {copy.message}
          </Alert>
        ) : null}
      </div>
    </Modal>
  );
}
