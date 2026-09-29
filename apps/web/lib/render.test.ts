import { describe, expect, it } from 'vitest';
import { job, renderProgressStatus, renderStatus } from '@/test/fixtures';
import {
  derivePhase,
  describeRenderProgress,
  jobIsFor,
  renderButtonLabel,
  renderFailureMessage,
  sceneStateView,
  summariseScenes,
  type RenderSelection,
} from './render';

const selection: RenderSelection = { orientation: 'horizontal', quality: 'low' };
const renderJob = (over: Parameters<typeof job>[0] = {}) =>
  job({
    kind: 'render_video',
    payload: { orientation: 'horizontal', quality: 'low' },
    ...over,
  });

describe('derivePhase - a video is only ever "complete" when it truly is', () => {
  it('is complete only when the API says the video is current', () => {
    const status = renderStatus({ is_complete: true, ready_count: 40, needs_render_count: 0 });
    expect(derivePhase(status, renderJob(), selection)).toBe('complete');
  });

  it('is never complete with sections missing, however many are done', () => {
    const status = renderStatus({ ready_count: 36, needs_render_count: 4, is_complete: false });
    expect(derivePhase(status, renderJob({ status: 'succeeded' }), selection)).not.toBe('complete');
    expect(derivePhase(status, renderJob({ status: 'succeeded' }), selection)).toBe('not_rendered');
  });

  it('is failed after a failed render for this selection', () => {
    const status = renderStatus({ ready_count: 36, failed_count: 4, needs_render_count: 0 });
    expect(derivePhase(status, renderJob({ status: 'failed' }), selection)).toBe('failed');
  });

  it('is failed when scenes are marked failed even if the job list is empty', () => {
    expect(derivePhase(renderStatus({ failed_count: 1 }), null, selection)).toBe('failed');
  });

  it('ignores a failed render made for a different shape or quality', () => {
    const other = renderJob({
      status: 'failed',
      payload: { orientation: 'vertical', quality: 'high' },
    });
    expect(derivePhase(renderStatus(), other, selection)).toBe('not_rendered');
  });

  it('is active while a render is queued or running, whatever else is true', () => {
    for (const status of ['queued', 'running'] as const) {
      expect(
        derivePhase(renderStatus({ is_complete: true }), renderJob({ status }), selection),
      ).toBe('active');
    }
  });

  it('is blocked when something upstream is not ready', () => {
    expect(derivePhase(renderStatus({ blockers: ['narrate first'] }), null, selection)).toBe(
      'blocked',
    );
  });

  it('is outdated when a video exists but scenes have changed since', () => {
    const status = renderStatus({
      output: {
        orientation: 'horizontal',
        quality: 'low',
        width: 960,
        height: 540,
        fps: 30,
        duration_ms: 1,
        byte_size: 1,
        encoder: 'libx264',
        renderer_version: 'v',
        created_at: '2026-09-26T10:00:00.000Z',
        updated_at: '2026-09-26T10:00:00.000Z',
        is_current: false,
      },
      stale_count: 1,
      is_complete: false,
    });
    expect(derivePhase(status, null, selection)).toBe('outdated');
  });

  it('is not rendered when nothing has been made', () => {
    expect(derivePhase(renderStatus(), null, selection)).toBe('not_rendered');
    expect(derivePhase(undefined, null, selection)).toBe('not_rendered');
  });
});

describe('jobIsFor', () => {
  it('matches on orientation and quality', () => {
    expect(jobIsFor(renderJob(), selection)).toBe(true);
    expect(jobIsFor(renderJob(), { ...selection, quality: 'high' })).toBe(false);
    expect(jobIsFor(null, selection)).toBe(false);
  });
});

describe('describeRenderProgress - real numbers only', () => {
  it('reads like "Section 7 of 37 · Scene 3 of 5 / 42 of 180 scenes ready"', () => {
    const view = describeRenderProgress({
      stage: 'rendering',
      segment_index: 6,
      segment_count: 37,
      scene_index: 2,
      scene_count: 5,
      chunks_total: 180,
      chunks_complete: 42,
      percent: 23,
      eta_seconds: 150,
    });
    expect(view.headline).toBe('Rendering video');
    expect(view.details).toEqual([
      'Section 7 of 37 · Scene 3 of 5',
      '42 of 180 scenes ready',
      'About 3 minutes left',
    ]);
    expect(view.percent).toBe(23);
  });

  it('says nothing it does not know', () => {
    const view = describeRenderProgress({ stage: 'assembling' });
    expect(view).toEqual({
      headline: 'Putting the video together',
      details: [],
      percent: undefined,
    });
  });

  it('waits honestly before the worker has reported anything', () => {
    expect(describeRenderProgress(null).headline).toBe('Waiting for a free renderer');
    expect(describeRenderProgress(renderProgressStatus().progress).percent).toBeUndefined();
  });

  it('never invents an estimate', () => {
    expect(
      describeRenderProgress({ stage: 'rendering', chunks_total: 1, chunks_complete: 0 }).details,
    ).toEqual(['0 of 1 scene ready']);
  });
});

describe('summariseScenes', () => {
  it('names the sections that need another attempt, in plain grammar', () => {
    expect(summariseScenes(renderStatus({ failed_count: 4, needs_render_count: 0 }))[0]?.text).toBe(
      '4 render sections need another attempt',
    );
    expect(summariseScenes(renderStatus({ failed_count: 1, needs_render_count: 0 }))[0]?.text).toBe(
      '1 render section needs another attempt',
    );
  });

  it('lists stale and unrendered scenes after failures', () => {
    const lines = summariseScenes(
      renderStatus({ failed_count: 1, stale_count: 2, needs_render_count: 1 }),
    );
    expect(lines.map((line) => line.tone)).toEqual(['danger', 'warning', 'info']);
    expect(lines[1]?.text).toBe('2 scenes changed since the last video');
    expect(lines[2]?.text).toBe('1 scene is not rendered yet');
  });

  it('is empty when every scene is ready', () => {
    expect(summariseScenes(renderStatus({ needs_render_count: 0, ready_count: 1 }))).toEqual([]);
  });
});

describe('labels', () => {
  it('words the main button for what pressing it will do', () => {
    const status = renderStatus({ stale_count: 2, needs_render_count: 1 });
    expect(renderButtonLabel('not_rendered', 'low', status)).toBe('Render preview');
    expect(renderButtonLabel('not_rendered', 'high', status)).toBe('Render video');
    expect(renderButtonLabel('failed', 'low', status)).toBe('Retry');
    expect(renderButtonLabel('outdated', 'high', status)).toBe('Update video (3 scenes)');
    expect(renderButtonLabel('complete', 'low', status)).toBe('Render preview again');
  });

  it('gives every scene state a word and a tone', () => {
    expect(sceneStateView('ready')).toEqual({ label: 'Ready', tone: 'success' });
    expect(sceneStateView('needs_render').label).toBe('Needs render');
    expect(sceneStateView('stale').tone).toBe('warning');
    expect(sceneStateView('failed').tone).toBe('danger');
  });

  it('never shows a technical failure message', () => {
    expect(
      renderFailureMessage(
        renderJob({ status: 'failed', error: 'Traceback (most recent call last)' }),
      ),
    ).toMatch(/could not be finished/);
    expect(
      renderFailureMessage(
        renderJob({ status: 'failed', error: '4 render sections need another attempt.' }),
      ),
    ).toBe('4 render sections need another attempt.');
  });
});
