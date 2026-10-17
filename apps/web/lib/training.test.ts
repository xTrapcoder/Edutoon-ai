import { describe, expect, it } from 'vitest';
import type { Attempt } from '@edutoon/contracts';
import { bestAttempt, scoreLabel } from './training';

const attempt = (over: Partial<Attempt> = {}): Attempt => ({
  id: 'a',
  module_id: 'm',
  attempt_number: 1,
  correct_count: 1,
  question_count: 2,
  threshold_percent: 70,
  passed: false,
  submitted_at: '2026-10-17T10:00:00.000Z',
  ...over,
});

describe('bestAttempt', () => {
  it('has nothing to show before any attempt', () => {
    expect(bestAttempt([])).toMatchObject({ count: 0, passed: false });
  });

  it('shows the highest score when none has passed, comparing whole numbers', () => {
    const summary = bestAttempt([
      attempt({ id: 'a', attempt_number: 1, correct_count: 1, question_count: 3 }),
      attempt({ id: 'b', attempt_number: 2, correct_count: 2, question_count: 3 }),
      attempt({ id: 'c', attempt_number: 3, correct_count: 1, question_count: 3 }),
    ]);
    expect(summary).toMatchObject({ count: 3, passed: false });
    expect(summary.best.id).toBe('b');
  });

  it('treats a pass as permanent and shows the first one', () => {
    const summary = bestAttempt([
      attempt({ id: 'fail', attempt_number: 1 }),
      attempt({ id: 'pass-2', attempt_number: 2, correct_count: 2, passed: true }),
      attempt({ id: 'later-fail', attempt_number: 3 }),
      attempt({ id: 'pass-4', attempt_number: 4, correct_count: 2, passed: true }),
    ]);
    expect(summary.passed).toBe(true);
    expect(summary.best.id).toBe('pass-2');
  });
});

describe('scoreLabel', () => {
  it('rounds the percentage down and never reaches 70% by rounding up', () => {
    expect(scoreLabel(attempt({ correct_count: 2, question_count: 3 }))).toBe(
      '2 of 3 correct (66%)',
    );
    expect(scoreLabel(attempt({ correct_count: 7, question_count: 10 }))).toBe(
      '7 of 10 correct (70%)',
    );
    expect(scoreLabel(attempt({ correct_count: 0, question_count: 0 }))).toBe(
      '0 of 0 correct (0%)',
    );
  });
});
