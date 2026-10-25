import type { Attempt } from '@edutoon/contracts';

/**
 * Small pure helpers for the training screens. Scores are whole numbers all the way from the API
 * (`correct_count`, `question_count`, `threshold_percent`); nothing here compares a fraction.
 */

export interface AttemptSummary {
  count: number;
  /** Whether any attempt passed - a pass is permanent. */
  passed: boolean;
  /** The attempt to show: the first pass if there is one, otherwise the highest score. */
  best: Attempt;
}

const EMPTY_ATTEMPT: Attempt = {
  id: '',
  module_id: '',
  attempt_number: 0,
  correct_count: 0,
  question_count: 0,
  threshold_percent: 0,
  passed: false,
  submitted_at: '',
};

/** The most telling attempt of a person's attempts at one module, and whether they have passed. */
export function bestAttempt(attempts: readonly Attempt[]): AttemptSummary {
  if (attempts.length === 0) return { count: 0, passed: false, best: EMPTY_ATTEMPT };
  const passes = attempts.filter((attempt) => attempt.passed);
  if (passes.length > 0) {
    const first = [...passes].sort((a, b) => a.attempt_number - b.attempt_number)[0]!;
    return { count: attempts.length, passed: true, best: first };
  }
  // Highest share correct, compared as integers: a * d > b * c, never a float.
  const best = attempts.reduce((top, attempt) =>
    attempt.correct_count * top.question_count > top.correct_count * attempt.question_count
      ? attempt
      : top,
  );
  return { count: attempts.length, passed: false, best };
}

/** "3 of 4 correct (75%)" - the percentage is rounded down, and shown only as a label. */
export function scoreLabel(attempt: Pick<Attempt, 'correct_count' | 'question_count'>): string {
  const percent =
    attempt.question_count === 0
      ? 0
      : Math.floor((attempt.correct_count * 100) / attempt.question_count);
  return `${attempt.correct_count} of ${attempt.question_count} correct (${percent}%)`;
}
