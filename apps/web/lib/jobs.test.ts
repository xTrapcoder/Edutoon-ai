import { describe, expect, it } from 'vitest';
import { job } from '@/test/fixtures';
import { assessWait, jobFailureMessage, readExtractionSummary } from './jobs';

const at = (iso: string) => Date.parse(iso);

describe('assessWait', () => {
  it('says nothing about a job that has only just started', () => {
    const running = job({ status: 'running', started_at: '2026-09-26T10:00:00Z' });
    expect(assessWait(running, at('2026-09-26T10:00:30Z'))).toMatchObject({
      level: 'normal',
      message: null,
    });
  });

  it('tells the person when a job has waited unusually long, instead of spinning silently', () => {
    const queued = job({ status: 'queued', created_at: '2026-09-26T10:00:00Z' });
    const wait = assessWait(queued, at('2026-09-26T10:03:00Z'));
    expect(wait.level).toBe('slow');
    expect(wait.message).toMatch(/nothing is lost/i);
  });

  it('escalates a job that has run far too long', () => {
    const running = job({ status: 'running', started_at: '2026-09-26T10:00:00Z' });
    expect(assessWait(running, at('2026-09-26T10:20:00Z')).level).toBe('stalled');
  });
});

describe('readExtractionSummary', () => {
  it('reads what the untyped result reports', () => {
    const summary = readExtractionSummary(
      job({
        result: {
          mode: 'pdf',
          topic_count: 12,
          truncated: true,
          chunks_total: 90,
          chunks_used: 40,
        },
      }),
    );
    expect(summary).toEqual({
      mode: 'pdf',
      topicCount: 12,
      truncated: true,
      chunksTotal: 90,
      chunksUsed: 40,
    });
  });

  it('never throws on a result that is missing or odd', () => {
    expect(readExtractionSummary(job({ result: null }))).toBeNull();
    expect(readExtractionSummary(job({ result: { mode: 42, topic_count: 'lots' } }))).toMatchObject(
      {
        mode: null,
        topicCount: null,
      },
    );
  });
});

describe('jobFailureMessage', () => {
  it('shows a failure written for people, and hides anything technical', () => {
    expect(
      jobFailureMessage(job({ status: 'failed', error: 'The model is busy right now.' })),
    ).toBe('The model is busy right now.');
    expect(
      jobFailureMessage(
        job({ status: 'failed', error: 'Traceback (most recent call last): boom' }),
      ),
    ).toBe("This step didn't finish. You can try again.");
  });
});
