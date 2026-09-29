import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  job,
  PROJECT_ID,
  renderOutput,
  renderProgressStatus,
  renderScene,
  renderStatus,
  scene,
  scriptDetail,
  segmentStoryboard,
  source,
  storyboardStatus,
  topic,
} from '@/test/fixtures';
import { renderProjectPage, serveProject } from '@/test/render';
import { API, server } from '@/test/server';
import { ScriptPage } from './script-view';

const base = `${API}/v1/projects/${PROJECT_ID}`;
const ready = {
  sources: [source()],
  topics: [topic()],
  jobs: [job({ kind: 'extract_topics' })],
};

const approvedDetail = scriptDetail({ status: 'approved', measured_duration_ms: 60_000 });
const [approvedSegment] = approvedDetail.segments;
if (!approvedSegment) throw new Error('fixture must have a segment');

const completeStoryboard = storyboardStatus({
  script: approvedDetail,
  segments: [
    segmentStoryboard({
      id: approvedSegment.id,
      is_ready: true,
      scenes: [
        scene({ id: '00000000-0000-4000-8000-0000000000a1', scene_index: 0 }),
        scene({
          id: '00000000-0000-4000-8000-0000000000a2',
          scene_index: 1,
          template: 'bullet_points',
          content: { title: 'Recap', bullets: ['Networks share resources', 'They use protocols'] },
        }),
      ],
    }),
  ],
  ready_segment_count: 1,
  not_ready_segment_ids: [],
  is_complete: true,
});

const scenes = (states: ('ready' | 'needs_render' | 'stale' | 'failed')[]) =>
  states.map((state, index) =>
    renderScene({ position: index, scene_index: index, segment_id: approvedSegment.id, state }),
  );

function serve(render: ReturnType<typeof renderStatus>, progress = renderProgressStatus()) {
  serveProject({
    ...ready,
    scripts: [approvedDetail],
    scriptDetail: approvedDetail,
    storyboardStatus: completeStoryboard,
    renderStatus: render,
    renderProgress: progress,
  });
}

beforeEach(() => {
  vi.stubGlobal(
    'URL',
    Object.assign(URL, { createObjectURL: () => 'blob:test-video', revokeObjectURL: () => {} }),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe('Video (Phase 10)', () => {
  it('is offered only once narration is fully measured', async () => {
    const unmeasured = scriptDetail({ status: 'approved', measured_duration_ms: null });
    serveProject({ ...ready, scripts: [unmeasured], scriptDetail: unmeasured });

    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByText(/narrate every section before rendering/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /render (preview|video)/i })).toBeNull();
  });

  it('explains what stands in the way and will not start a render that cannot work', async () => {
    serve(
      renderStatus({
        blockers: [
          'The storyboard is incomplete: 1 section still needs scenes. Generate the storyboard first.',
        ],
        scenes: [],
        needs_render_count: 0,
        estimate: null,
      }),
    );
    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByText('Not ready to render yet')).toBeInTheDocument();
    expect(screen.getByText(/generate the storyboard first/i)).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'Render preview' })).toBeDisabled();
  });

  it('shows the estimate, asks for a shape and quality, and queues the render with them', async () => {
    let body: unknown;
    let key: string | null = null;
    serve(
      renderStatus({ scenes: scenes(['needs_render', 'needs_render']), needs_render_count: 2 }),
    );
    server.use(
      http.post(`${base}/scripts/:id/render`, async ({ request }) => {
        body = await request.json();
        key = request.headers.get('idempotency-key');
        return HttpResponse.json(
          {
            job: job({ kind: 'render_video', status: 'queued' }),
            estimate: renderStatus().estimate,
          },
          { status: 202 },
        );
      }),
    );
    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByText(/1 of 1 scenes to render/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('radio', { name: 'Vertical' }));
    await userEvent.click(screen.getByRole('radio', { name: 'Final' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Render video' }));

    await waitFor(() => expect(body).toEqual({ orientation: 'vertical', quality: 'high' }));
    expect(key).toBeTruthy();
  });

  it('asks the API for the selected shape and quality', async () => {
    const seen: string[] = [];
    serve(renderStatus());
    server.use(
      http.get(`${base}/scripts/:id/render`, ({ request }) => {
        const url = new URL(request.url);
        seen.push(`${url.searchParams.get('orientation')}/${url.searchParams.get('quality')}`);
        return HttpResponse.json(renderStatus());
      }),
    );
    renderProjectPage(<ScriptPage />, 'script');

    await screen.findByRole('button', { name: 'Render preview' });
    await userEvent.click(screen.getByRole('radio', { name: 'Vertical' }));
    await waitFor(() => expect(seen).toContain('vertical/low'));
    expect(seen[0]).toBe('horizontal/low');
  });

  it('shows real progress while rendering: section, scene, counts and percentage', async () => {
    serve(
      renderStatus({
        scenes: scenes(['ready', 'needs_render']),
        ready_count: 1,
        needs_render_count: 1,
      }),
      renderProgressStatus({
        job: job({
          kind: 'render_video',
          status: 'running',
          payload: { orientation: 'horizontal', quality: 'low' },
        }),
        progress: {
          stage: 'rendering',
          segment_index: 6,
          segment_count: 37,
          scene_index: 2,
          scene_count: 5,
          chunks_total: 180,
          chunks_complete: 42,
          percent: 23,
        },
      }),
    );
    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByText('Rendering video')).toBeInTheDocument();
    expect(screen.getByText('Section 7 of 37 · Scene 3 of 5')).toBeInTheDocument();
    expect(screen.getByText('42 of 180 scenes ready')).toBeInTheDocument();
    expect(screen.getByRole('progressbar', { name: 'Render progress' })).toHaveAttribute(
      'aria-valuenow',
      '23',
    );
    expect(screen.queryByRole('button', { name: /render (preview|video)/i })).toBeNull();
  });

  it('does not call a partly-failed render finished, and says how much is left', async () => {
    serve(
      renderStatus({
        scenes: scenes(['ready', 'failed', 'failed', 'failed', 'failed']),
        ready_count: 1,
        failed_count: 4,
        needs_render_count: 0,
      }),
      renderProgressStatus({
        job: job({
          kind: 'render_video',
          status: 'failed',
          error:
            '4 render sections need another attempt. Everything already rendered is kept - try again to finish.',
          payload: { orientation: 'horizontal', quality: 'low' },
        }),
      }),
    );
    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByText('4 render sections need another attempt')).toBeInTheDocument();
    expect(screen.queryByText(/up to date/i)).toBeNull();
    expect(screen.queryByLabelText('Your video')).toBeNull();
    expect(await screen.findByRole('button', { name: 'Retry' })).toBeEnabled();
  });

  it('retries the failed render with an idempotency key', async () => {
    let retried = false;
    let key: string | null = null;
    serve(
      renderStatus({ scenes: scenes(['failed']), failed_count: 1, needs_render_count: 0 }),
      renderProgressStatus({
        job: job({
          kind: 'render_video',
          status: 'failed',
          error: null,
          payload: { orientation: 'horizontal', quality: 'low' },
        }),
      }),
    );
    server.use(
      http.post(`${base}/render/retry`, ({ request }) => {
        retried = true;
        key = request.headers.get('idempotency-key');
        return HttpResponse.json(
          {
            job: job({ kind: 'render_video', status: 'queued' }),
            estimate: renderStatus().estimate,
          },
          { status: 202 },
        );
      }),
    );
    renderProjectPage(<ScriptPage />, 'script');

    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(retried).toBe(true));
    expect(key).toBeTruthy();
  });

  it('shows the plain reason when a render cannot start', async () => {
    serve(renderStatus());
    server.use(
      http.post(`${base}/scripts/:id/render`, () =>
        HttpResponse.json(
          {
            error: {
              code: 'conflict',
              message:
                'A video render is already queued or running for this project; wait for it to finish.',
            },
          },
          { status: 409 },
        ),
      ),
    );
    renderProjectPage(<ScriptPage />, 'script');

    await userEvent.click(await screen.findByRole('button', { name: 'Render preview' }));
    expect(await screen.findByText(/already queued or running/i)).toBeInTheDocument();
  });

  it('shows a finished, current video with a player and a download', async () => {
    server.use(
      http.get(`${base}/scripts/:id/render/video`, () =>
        HttpResponse.arrayBuffer(new Uint8Array([0, 0, 0, 24, 102, 116, 121, 112]).buffer, {
          headers: { 'content-type': 'video/mp4' },
        }),
      ),
    );
    serve(
      renderStatus({
        scenes: scenes(['ready', 'ready']),
        ready_count: 2,
        needs_render_count: 0,
        is_complete: true,
        output_is_current: true,
        output: renderOutput(),
      }),
    );
    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByText('Video: Up to date')).toBeInTheDocument();
    const video = await screen.findByLabelText('Your video', { selector: 'video' });
    expect(video).toHaveAttribute('src', 'blob:test-video');
    expect(video).toHaveAttribute('controls');
    const download = screen.getByRole('link', { name: /download mp4/i });
    expect(download).toHaveAttribute('download', 'video-horizontal-low.mp4');
    expect(screen.getByText(/960 × 540/)).toBeInTheDocument();
  });

  it('marks an out-of-date video as such, offers the previous one, and updates only what changed', async () => {
    server.use(
      http.get(`${base}/scripts/:id/render/video`, () =>
        HttpResponse.arrayBuffer(new Uint8Array([1, 2, 3]).buffer, {
          headers: { 'content-type': 'video/mp4' },
        }),
      ),
    );
    serve(
      renderStatus({
        scenes: scenes(['ready', 'stale']),
        ready_count: 1,
        stale_count: 1,
        needs_render_count: 0,
        is_complete: false,
        output_is_current: false,
        output: renderOutput({ is_current: false }),
      }),
    );
    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByText('This video is out of date')).toBeInTheDocument();
    expect(screen.getByText(/1 scene changed since the last video/i)).toBeInTheDocument();
    expect(screen.queryByText(/up to date/i)).toBeNull();
    expect(
      await screen.findByLabelText('Previous version', { selector: 'video' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Update preview (1 scene)' })).toBeInTheDocument();
  });

  it("shows each scene's own render state in the storyboard, so one edit marks only one scene", async () => {
    serve(
      renderStatus({
        scenes: scenes(['ready', 'stale']),
        ready_count: 1,
        stale_count: 1,
        needs_render_count: 0,
        output: renderOutput({ is_current: false }),
      }),
    );
    server.use(
      http.get(`${base}/scripts/:id/render/video`, () =>
        HttpResponse.arrayBuffer(new Uint8Array([1]).buffer, {
          headers: { 'content-type': 'video/mp4' },
        }),
      ),
    );
    renderProjectPage(<ScriptPage />, 'script');

    const ready = await screen.findAllByLabelText('Render state: Ready');
    const stale = await screen.findAllByLabelText('Render state: Stale');
    expect(ready).toHaveLength(1);
    expect(stale).toHaveLength(1);
    expect(screen.queryByLabelText('Render state: Failed')).toBeNull();
  });

  it('keeps the card usable on a narrow screen: nothing forces it wider than its column', async () => {
    serve(renderStatus());
    renderProjectPage(<ScriptPage />, 'script');
    const button = await screen.findByRole('button', { name: 'Render preview' });
    expect(button.className).toContain('w-full');
    for (const radio of screen.getAllByRole('radio')) {
      expect(radio.closest('fieldset')?.className).toContain('min-w-0');
    }
  });
});
