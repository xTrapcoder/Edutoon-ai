import { describe, expect, it } from 'vitest';
import { job, project, script, source, topic } from '@/test/fixtures';
import { deriveStage, deriveWorkflow, type StageId } from './workflow';

const pdf = project({ source_type: 'pdf' });
const keywords = project({ source_type: 'topic' });

type Input = Parameters<typeof deriveStage>[0];

describe('deriveStage', () => {
  const cases: [string, Input, StageId][] = [
    [
      'a PDF project with no PDF yet',
      { project: pdf, sources: [], topics: [], jobs: [], scripts: [] },
      'needs_source',
    ],
    [
      'a PDF that could not be read',
      { project: pdf, sources: [source({ status: 'failed' })], topics: [], jobs: [], scripts: [] },
      'source_failed',
    ],
    [
      'a PDF still being read',
      {
        project: pdf,
        sources: [source({ status: 'processing' })],
        topics: [],
        jobs: [],
        scripts: [],
      },
      'reading_source',
    ],
    [
      'a parsed PDF with nothing extracted',
      { project: pdf, sources: [source()], topics: [], jobs: [], scripts: [] },
      'ready_for_topics',
    ],
    [
      'a keyword-only project with no keywords (not supported yet)',
      { project: keywords, sources: [], topics: [], jobs: [], scripts: [] },
      'keywords_unsupported',
    ],
    [
      'a keyword-only project with keywords is a dead end, not a way forward',
      {
        project: keywords,
        sources: [],
        topics: [topic({ origin: 'user' })],
        jobs: [],
        scripts: [],
      },
      'keywords_unsupported',
    ],
    [
      'a keyword-only project whose data has not loaded yet',
      { project: keywords },
      'keywords_unsupported',
    ],
    [
      'an extraction that is queued',
      {
        project: pdf,
        sources: [source()],
        topics: [],
        jobs: [job({ status: 'queued' })],
        scripts: [],
      },
      'extracting_topics',
    ],
    [
      'an extraction that is running',
      {
        project: pdf,
        sources: [source()],
        topics: [],
        jobs: [job({ status: 'running' })],
        scripts: [],
      },
      'extracting_topics',
    ],
    [
      'a failed extraction with nothing to show',
      {
        project: pdf,
        sources: [source()],
        topics: [],
        jobs: [job({ status: 'failed' })],
        scripts: [],
      },
      'topics_failed',
    ],
    [
      'a failed re-run that left the earlier topics intact',
      {
        project: pdf,
        sources: [source()],
        topics: [topic()],
        jobs: [job({ status: 'failed' })],
        scripts: [],
      },
      'topics_ready',
    ],
    [
      'a finished extraction',
      { project: pdf, sources: [source()], topics: [topic()], jobs: [job()], scripts: [] },
      'topics_ready',
    ],
    [
      'a script being written',
      {
        project: pdf,
        sources: [source()],
        topics: [topic()],
        jobs: [job()],
        scripts: [script({ status: 'pending' })],
      },
      'writing_script',
    ],
    [
      'a draft script',
      {
        project: pdf,
        sources: [source()],
        topics: [topic()],
        jobs: [job()],
        scripts: [script({ status: 'draft' })],
      },
      'script_ready',
    ],
    [
      'a failed script',
      {
        project: pdf,
        sources: [source()],
        topics: [topic()],
        jobs: [job()],
        scripts: [script({ status: 'failed' })],
      },
      'script_failed',
    ],
    [
      'an approved script',
      {
        project: pdf,
        sources: [source()],
        topics: [topic()],
        jobs: [job()],
        scripts: [script({ status: 'approved' })],
      },
      'approved',
    ],
  ];

  it.each(cases)('%s', (_name, input, expected) => {
    expect(deriveStage(input)).toBe(expected);
  });

  it('ignores superseded scripts, so an old approval never hides a newer draft', () => {
    const scripts = [
      script({ id: 'new', status: 'draft', created_at: '2026-09-26T12:00:00Z' }),
      script({ id: 'old', status: 'superseded', created_at: '2026-09-26T09:00:00Z' }),
    ];
    expect(
      deriveStage({ project: pdf, sources: [source()], topics: [topic()], jobs: [job()], scripts }),
    ).toBe('script_ready');
  });

  it('says no more than "draft" for what it cannot know yet, rather than guessing', () => {
    expect(deriveStage({ project: pdf })).toBe('unknown');
    expect(deriveStage({ project: pdf, jobs: [], scripts: [] })).toBe('unknown');
  });
});

describe('deriveWorkflow', () => {
  it('never lets a later stage be opened early', () => {
    const early = deriveWorkflow({
      project: pdf,
      sources: [source()],
      topics: [],
      jobs: [],
      scripts: [],
    });
    expect(early.steps.map((step) => [step.id, step.reachable])).toEqual([
      ['source', true],
      ['topics', true],
      ['script', false],
      ['approved', false],
    ]);
  });

  it('opens the script only once there is a topic to write from', () => {
    const rejected = topic({ id: 'r', status: 'rejected' });
    const none = deriveWorkflow({
      project: pdf,
      sources: [source()],
      topics: [rejected],
      jobs: [job()],
      scripts: [],
    });
    expect(none.canOpenScript).toBe(false);
    const some = deriveWorkflow({
      project: pdf,
      sources: [source()],
      topics: [rejected, topic({ id: 'b' })],
      jobs: [job()],
      scripts: [],
    });
    expect(some.canOpenScript).toBe(true);
  });

  it('marks every step complete once the script is approved', () => {
    const done = deriveWorkflow({
      project: pdf,
      sources: [source()],
      topics: [topic()],
      jobs: [job()],
      scripts: [script({ status: 'approved' })],
    });
    expect(done.steps.every((step) => step.state === 'complete')).toBe(true);
  });

  it('flags the current step when it failed', () => {
    const failed = deriveWorkflow({
      project: pdf,
      sources: [source()],
      topics: [],
      jobs: [job({ status: 'failed' })],
      scripts: [],
    });
    expect(failed.steps.find((step) => step.state === 'current')).toMatchObject({
      id: 'topics',
      attention: true,
    });
  });
});
