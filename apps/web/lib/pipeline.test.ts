import { describe, expect, it } from 'vitest';
import {
  claim,
  job,
  project,
  renderOutput,
  renderProgressStatus,
  renderStatus,
  script,
  scriptDetail,
  segmentAudio,
  segmentStoryboard,
  scene,
  source,
  storyboardStatus,
  synthesisStatus,
  topic,
} from '@/test/fixtures';
import {
  derivePipeline,
  pickRenderStatus,
  PIPELINE_ORDER,
  type PipelineInput,
  type PipelineStageId,
} from './pipeline';
import { deriveWorkflow } from './workflow';

const pdf = project({ source_type: 'pdf' });

/** Build the pipeline for a project from what the API would return. */
function pipeline(over: Partial<PipelineInput> & { project?: typeof pdf } = {}) {
  const base = {
    project: over.project ?? pdf,
    sources: [source()],
    topics: [topic({ status: 'accepted' })],
    jobs: [job({ kind: 'extract_topics' })],
    scripts: [],
    claims: [claim()],
    ...over,
  };
  const workflow = deriveWorkflow(base);
  return derivePipeline({ ...base, workflow });
}

const statuses = (stages: ReturnType<typeof pipeline>) =>
  Object.fromEntries(stages.map((stage) => [stage.id, stage.status])) as Record<
    PipelineStageId,
    string
  >;

const approved = script({ status: 'approved', measured_duration_ms: 60_000 });
const detail = scriptDetail({ status: 'approved', measured_duration_ms: 60_000 });
const [segment] = detail.segments;
if (!segment) throw new Error('fixture must have a segment');

const narrated = synthesisStatus({
  script: approved,
  segments: [
    {
      id: segment.id,
      segment_index: 0,
      estimated_duration_ms: 60_000,
      audio: segmentAudio({ measured_duration_ms: 58_000 }),
    },
  ],
});
const storyboardDone = storyboardStatus({
  script: approved,
  segments: [
    segmentStoryboard({ id: segment.id, is_ready: true, scenes: [scene(), scene({ id: 'x' })] }),
  ],
  ready_segment_count: 1,
  not_ready_segment_ids: [],
  is_complete: true,
});

describe('derivePipeline', () => {
  it('lists the eight stages in order, each linking to its own page', () => {
    const stages = pipeline();
    expect(stages.map((stage) => stage.id)).toEqual([...PIPELINE_ORDER]);
    expect(stages.map((stage) => stage.label)).toEqual([
      'Source',
      'Settings',
      'Topics',
      'Claims',
      'Script',
      'Narration',
      'Storyboard',
      'Video',
    ]);
    expect(stages.find((stage) => stage.id === 'video')?.href).toMatch(/\/video$/);
    expect(stages.find((stage) => stage.id === 'narration')?.href).toMatch(/\/narration$/);
  });

  it('shows nothing as completed for a project with no PDF, and blocks every later stage', () => {
    const result = statuses(
      pipeline({ sources: [], topics: [], jobs: [], claims: [], scripts: [] }),
    );
    expect(result).toEqual({
      source: 'pending',
      settings: 'completed',
      topics: 'blocked',
      claims: 'blocked',
      script: 'blocked',
      narration: 'blocked',
      storyboard: 'blocked',
      video: 'blocked',
    });
  });

  it('reports the real project settings, never an invented one', () => {
    const stages = pipeline({
      project: project({ orientation: 'vertical', language: 'en-GB', target_duration_ms: 180_000 }),
    });
    expect(stages.find((stage) => stage.id === 'settings')?.detail).toBe(
      'Vertical · 3 minutes · English (UK)',
    );
  });

  it('moves from source to claims as the API reports each stage', () => {
    expect(statuses(pipeline({ sources: [source({ status: 'processing' })] })).source).toBe(
      'processing',
    );
    expect(statuses(pipeline({ sources: [source({ status: 'failed' })] })).source).toBe('failed');
    const result = statuses(pipeline());
    expect(result.source).toBe('completed');
    expect(result.topics).toBe('completed');
    expect(result.claims).toBe('completed');
    expect(result.script).toBe('pending');
  });

  it('marks running and failed jobs as such', () => {
    expect(
      statuses(pipeline({ topics: [], jobs: [job({ kind: 'extract_topics', status: 'running' })] }))
        .topics,
    ).toBe('processing');
    expect(
      statuses(pipeline({ topics: [], jobs: [job({ kind: 'extract_topics', status: 'failed' })] }))
        .topics,
    ).toBe('failed');
    expect(
      statuses(
        pipeline({
          claims: [],
          jobs: [
            job({ kind: 'extract_topics' }),
            job({ kind: 'extract_claims', status: 'queued' }),
          ],
        }),
      ).claims,
    ).toBe('processing');
  });

  it('does not count claims as done when none is supported', () => {
    const stages = pipeline({ claims: [claim({ status: 'unsupported' })] });
    expect(statuses(stages).claims).toBe('pending');
    expect(stages.find((stage) => stage.id === 'claims')?.detail).toBe(
      '1 claim checked · none supported yet',
    );
    expect(statuses(stages).script).toBe('blocked');
  });

  it('counts general-knowledge statements of a topic-only lesson as done, and says what they are', () => {
    const stages = pipeline({
      claims: [claim({ status: 'general_knowledge' }), claim({ status: 'general_knowledge' })],
    });
    expect(statuses(stages).claims).toBe('completed');
    expect(stages.find((stage) => stage.id === 'claims')?.detail).toBe(
      '2 statements from general knowledge · not checked against a document',
    );
    expect(statuses(stages).script).toBe('pending');
  });

  it('holds narration back until the script is approved', () => {
    const draft = pipeline({ scripts: [script({ status: 'draft' })] });
    expect(statuses(draft).script).toBe('pending');
    expect(statuses(draft).narration).toBe('blocked');
    expect(draft.find((stage) => stage.id === 'narration')?.blocker).toBe(
      'Approve the script first.',
    );
  });

  it('reports narration from the measured duration, the API gate for a narrated script', () => {
    const notYet = pipeline({
      scripts: [approved],
      synthesis: synthesisStatus({ script: script({ status: 'approved' }) }),
    });
    expect(statuses(notYet).script).toBe('completed');
    expect(statuses(notYet).narration).toBe('pending');
    expect(statuses(notYet).storyboard).toBe('blocked');

    const done = pipeline({ scripts: [approved], synthesis: narrated });
    expect(statuses(done).narration).toBe('completed');
    expect(done.find((stage) => stage.id === 'narration')?.detail).toBe('1 section · 1 minute');

    const running = pipeline({
      scripts: [approved],
      synthesis: synthesisStatus({
        script: script({ status: 'approved' }),
        job: job({ kind: 'synthesise_voice', status: 'running' }),
      }),
    });
    expect(statuses(running).narration).toBe('processing');
  });

  it('reports the storyboard once narration is complete', () => {
    expect(statuses(pipeline({ scripts: [approved], synthesis: narrated })).storyboard).toBe(
      'checking',
    );
    const stages = pipeline({
      scripts: [approved],
      synthesis: narrated,
      storyboard: storyboardDone,
    });
    expect(statuses(stages).storyboard).toBe('completed');
    expect(stages.find((stage) => stage.id === 'storyboard')?.detail).toBe('2 scenes · 1 section');
    expect(statuses(stages).video).toBe('checking');
  });

  it('shows the finished video with the fps the API reports, and a running render as processing', () => {
    const base = { scripts: [approved], synthesis: narrated, storyboard: storyboardDone };
    const finished = pipeline({
      ...base,
      render: renderStatus({
        is_complete: true,
        output: renderOutput({ quality: 'high', width: 1920, height: 1080, fps: 60 }),
        quality: 'high',
      }),
      renderJob: null,
    });
    expect(statuses(finished).video).toBe('completed');
    expect(finished.find((stage) => stage.id === 'video')?.detail).toContain('60 FPS');
    expect(finished.find((stage) => stage.id === 'video')?.detail).toContain('1920×1080');

    const rendering = pipeline({
      ...base,
      render: renderStatus(),
      renderJob: job({ kind: 'render_video', status: 'running' }),
      renderProgress: renderProgressStatus().progress,
    });
    expect(statuses(rendering).video).toBe('processing');

    expect(statuses(pipeline({ ...base, render: renderStatus(), renderJob: null })).video).toBe(
      'pending',
    );
  });

  it("passes the render guard's own blocker through instead of hiding it", () => {
    const stages = pipeline({
      scripts: [approved],
      synthesis: narrated,
      storyboard: storyboardDone,
      render: renderStatus({ blockers: ['There is not enough free disk space.'] }),
      renderJob: null,
    });
    const video = stages.find((stage) => stage.id === 'video');
    expect(video?.status).toBe('blocked');
    expect(video?.blocker).toBe('There is not enough free disk space.');
  });

  it('says a stage could not be checked rather than waiting for ever', () => {
    const stages = pipeline({
      scripts: [approved],
      failed: new Set<PipelineStageId>(['narration']),
    });
    expect(statuses(stages).narration).toBe('unknown');
  });
});

describe('pickRenderStatus', () => {
  const none = renderStatus({ output: null });
  const finished = renderStatus({
    quality: 'high',
    is_complete: true,
    output: renderOutput({ quality: 'high' }),
  });
  const stale = renderStatus({ output: renderOutput(), is_complete: false });

  it('prefers a finished, current video over one that merely exists', () => {
    expect(pickRenderStatus([stale, finished])).toBe(finished);
    expect(pickRenderStatus([none, stale])).toBe(stale);
    expect(pickRenderStatus([none, undefined])).toBe(none);
    expect(pickRenderStatus([undefined, undefined])).toBeUndefined();
  });
});
