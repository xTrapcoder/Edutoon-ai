import { describe, expect, it } from 'vitest';
import { job } from '@/test/fixtures';
import {
  assessWait,
  jobFailureMessage,
  readClaimExtractionSummary,
  readExtractionSummary,
  readScriptChecks,
} from './jobs';

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

describe('readClaimExtractionSummary', () => {
  it('reads what the untyped result reports, screening each failure the same way job.error is', () => {
    const summary = readClaimExtractionSummary(
      job({
        kind: 'extract_claims',
        result: {
          topics_eligible: 2,
          topics_processed: 1,
          topics_skipped_already_extracted: 0,
          topics_failed: 1,
          candidates_generated: 4,
          claims_verified: 4,
          claims_by_status: { supported: 3, unsupported: 1 },
          failures: [
            {
              topic_id: 't1',
              topic_title: 'Devices',
              error: 'Traceback (most recent call last): boom',
            },
          ],
        },
      }),
    );
    expect(summary).toEqual({
      topicsEligible: 2,
      topicsProcessed: 1,
      topicsSkipped: 0,
      topicsFailed: 1,
      candidatesGenerated: 4,
      claimsVerified: 4,
      claimsByStatus: { supported: 3, unsupported: 1 },
      failures: [
        {
          topicId: 't1',
          topicTitle: 'Devices',
          error: "This step didn't finish. You can try again.",
        },
      ],
    });
  });

  it('never throws on a result that is missing or odd', () => {
    expect(readClaimExtractionSummary(job({ kind: 'extract_claims', result: null }))).toBeNull();
    expect(
      readClaimExtractionSummary(
        job({ kind: 'extract_claims', result: { topics_eligible: 'lots', failures: 'nope' } }),
      ),
    ).toMatchObject({ topicsEligible: null, failures: [] });
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

describe('readScriptChecks', () => {
  const scriptJob = (result: Record<string, unknown>) =>
    job({ kind: 'generate_script', status: 'succeeded', result: { script_id: 's1', ...result } });

  it('reads what the sentence check removed, how many it judged and which topics fell back', () => {
    const checks = readScriptChecks(
      [
        scriptJob({
          general_knowledge: false,
          support_check: {
            enabled: true,
            judged_sentences: 45,
            unchecked_sentences: 2,
            removed_sentences: [
              {
                sentence: 'Plants need water to open their stomata.',
                reason: 'reversed',
                topic: 'Water',
              },
              { nonsense: true },
            ],
            fallback_sections: [{ topic: 'Hotspots' }, 7],
          },
        }),
      ],
      's1',
    );
    expect(checks).toEqual({
      generalKnowledge: false,
      support: {
        enabled: true,
        judgedSentences: 45,
        uncheckedSentences: 2,
        removed: [
          {
            sentence: 'Plants need water to open their stomata.',
            reason: 'reversed',
            topic: 'Water',
            segmentDropped: false,
          },
        ],
        fallbackTopics: ['Hotspots'],
      },
    });
  });

  it('knows a general-knowledge lesson, and never confuses "no check ran" with "nothing found"', () => {
    expect(readScriptChecks([scriptJob({ general_knowledge: true })], 's1')).toEqual({
      generalKnowledge: true,
      support: null,
    });
    expect(readScriptChecks([scriptJob({})], 'other-script')).toBeNull();
    expect(readScriptChecks(undefined, 's1')).toBeNull();
  });
});
