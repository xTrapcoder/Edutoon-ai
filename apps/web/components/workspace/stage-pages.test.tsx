import { screen, within } from '@testing-library/react';
import axe from 'axe-core';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  claim,
  job,
  PROJECT_ID,
  project,
  renderOutput,
  renderScene,
  renderStatus,
  scene,
  scriptDetail,
  segmentAudio,
  segmentStoryboard,
  source,
  storyboardStatus,
  synthesisStatus,
  topic,
} from '@/test/fixtures';
import { renderProjectPage, serveProject } from '@/test/render';
import { API, server } from '@/test/server';
import { ClaimsPage, NarrationPage, StoryboardPage, VideoPage } from './stage-pages';

const base = `${API}/v1/projects/${PROJECT_ID}`;
const ready = {
  project: project({ orientation: 'horizontal', language: 'en-IN', target_duration_ms: 300_000 }),
  sources: [source()],
  topics: [topic({ status: 'accepted' })],
  jobs: [job({ kind: 'extract_topics' })],
  claims: [claim({ claim_text: 'A network connects computers.' })],
};

const approved = scriptDetail({ status: 'approved', measured_duration_ms: 60_000 });
const [segment] = approved.segments;
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

const completeStoryboard = storyboardStatus({
  script: approved,
  segments: [
    segmentStoryboard({
      id: segment.id,
      is_ready: true,
      scenes: [
        scene({ id: '00000000-0000-4000-8000-0000000000a1', scene_index: 0 }),
        scene({
          id: '00000000-0000-4000-8000-0000000000a2',
          scene_index: 1,
          template: 'bullet_points',
          content: { title: 'Recap', bullets: ['Networks share resources'] },
        }),
      ],
    }),
  ],
  ready_segment_count: 1,
  not_ready_segment_ids: [],
  is_complete: true,
});

const finishedVideo = renderStatus({
  quality: 'low',
  scenes: [renderScene({ segment_id: segment.id, state: 'ready' })],
  ready_count: 1,
  needs_render_count: 0,
  is_complete: true,
  output_is_current: true,
  output: renderOutput({ fps: 60 }),
});

const everything = {
  ...ready,
  scripts: [approved],
  scriptDetail: approved,
  synthesisStatus: narrated,
  storyboardStatus: completeStoryboard,
  renderStatus: finishedVideo,
};

function serveMedia() {
  server.use(
    http.get(`${base}/scripts/:id/render/video`, () =>
      HttpResponse.arrayBuffer(new Uint8Array([0, 0, 0, 24, 102, 116, 121, 112]).buffer, {
        headers: { 'content-type': 'video/mp4' },
      }),
    ),
    http.get(`${base}/scripts/:id/segments/:segmentId/audio`, () =>
      HttpResponse.arrayBuffer(new Uint8Array([82, 73, 70, 70]).buffer, {
        headers: { 'content-type': 'audio/wav' },
      }),
    ),
  );
}

beforeEach(() => {
  vi.stubGlobal(
    'URL',
    Object.assign(URL, { createObjectURL: () => 'blob:test-media', revokeObjectURL: () => {} }),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe('the eight-stage pipeline', () => {
  it('shows every stage of a finished project with the status the API reports', async () => {
    serveProject(everything);
    serveMedia();
    renderProjectPage(<VideoPage />, 'video');

    const stepper = await screen.findByRole('list', { name: 'Project progress' });
    const items = within(stepper).getAllByRole('listitem');
    expect(items.map((item) => within(item).getByRole('link').textContent)).toEqual([
      expect.stringContaining('Source'),
      expect.stringContaining('Settings'),
      expect.stringContaining('Topics'),
      expect.stringContaining('Claims'),
      expect.stringContaining('Script'),
      expect.stringContaining('Narration'),
      expect.stringContaining('Storyboard'),
      expect.stringContaining('Video'),
    ]);
    // Wait for the later stages, which are read only once the earlier ones are complete.
    await within(items[7]!).findByText('Completed');
    for (const item of items) expect(within(item).getByText('Completed')).toBeInTheDocument();
    expect(items[7]).toHaveTextContent(/60 FPS/);
  });

  it('never calls a stage complete while an earlier one is not', async () => {
    const unnarrated = scriptDetail({ status: 'approved', measured_duration_ms: null });
    serveProject({ ...ready, scripts: [unnarrated], scriptDetail: unnarrated });
    renderProjectPage(<VideoPage />, 'video');

    const stepper = await screen.findByRole('list', { name: 'Project progress' });
    const items = within(stepper).getAllByRole('listitem');
    expect(await within(items[5]!).findByText('Pending')).toBeInTheDocument(); // narration
    expect(within(items[6]!).getByText('Blocked')).toBeInTheDocument(); // storyboard
    expect(within(items[7]!).getByText('Blocked')).toBeInTheDocument(); // video
  });
});

describe('Video page', () => {
  it('plays the real video with the browser controls, offers the download and shows the settings', async () => {
    serveProject(everything);
    serveMedia();
    renderProjectPage(<VideoPage />, 'video');

    const video = await screen.findByLabelText('Your video', { selector: 'video' });
    expect(video).toHaveAttribute('src', 'blob:test-media');
    expect(video).toHaveAttribute('controls');
    expect(screen.getByRole('link', { name: /download mp4/i })).toHaveAttribute(
      'href',
      'blob:test-media',
    );

    const settings = screen
      .getByRole('heading', { name: 'Video settings' })
      .closest('.shadow-card') as HTMLElement;
    expect(within(settings).getByText('Horizontal')).toBeInTheDocument();
    expect(within(settings).getByText('5 minutes')).toBeInTheDocument();
    expect(within(settings).getByText('English (India)')).toBeInTheDocument();
    expect(await within(settings).findByText('60 FPS')).toBeInTheDocument();
  });

  it('is blocked, with the reason, until the earlier stages are complete - and offers no render', async () => {
    const unnarrated = scriptDetail({ status: 'approved', measured_duration_ms: null });
    serveProject({ ...ready, scripts: [unnarrated], scriptDetail: unnarrated });
    renderProjectPage(<VideoPage />, 'video');

    expect(await screen.findByText('Blocked — prerequisite required')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Complete the storyboard first.');
    expect(screen.getByRole('link', { name: /go to storyboard/i })).toHaveAttribute(
      'href',
      `/projects/${PROJECT_ID}/storyboard`,
    );
    expect(screen.queryByRole('button', { name: /render (preview|video)/i })).toBeNull();
  });

  it('says the script must be approved when it is only a draft', async () => {
    const draft = scriptDetail({ status: 'draft' });
    serveProject({ ...ready, scripts: [draft], scriptDetail: draft });
    renderProjectPage(<VideoPage />, 'video');

    expect(await screen.findByText('Blocked — prerequisite required')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /render/i })).toBeNull();
  });
});

describe('Narration page', () => {
  it('lists every section with its real audio, a player and a download', async () => {
    serveProject(everything);
    serveMedia();
    renderProjectPage(<NarrationPage />, 'narration');

    expect(
      await screen.findByRole('heading', { name: 'Narration by section' }),
    ).toBeInTheDocument();
    expect(await screen.findByText('All sections narrated')).toBeInTheDocument();
    const download = await screen.findByRole('link', { name: /download audio/i });
    expect(download).toHaveAttribute('download', 'narration-section-1.wav');
    expect(document.querySelector('audio[controls]')).not.toBeNull();
  });

  it('is blocked until the script is approved', async () => {
    serveProject({ ...ready, scripts: [scriptDetail({ status: 'draft' })] });
    renderProjectPage(<NarrationPage />, 'narration');

    expect(await screen.findByText('Blocked — prerequisite required')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Approve the script first.');
    expect(screen.queryByRole('button', { name: /narrate this script/i })).toBeNull();
  });
});

describe('Storyboard page', () => {
  it('shows the real scenes of a complete storyboard', async () => {
    serveProject(everything);
    renderProjectPage(<StoryboardPage />, 'storyboard');

    expect(await screen.findByText('Recap')).toBeInTheDocument();
    expect(screen.getByText('Networks share resources')).toBeInTheDocument();
  });

  it('is blocked, and says why, before the narration is complete', async () => {
    const unnarrated = scriptDetail({ status: 'approved', measured_duration_ms: null });
    serveProject({ ...ready, scripts: [unnarrated], scriptDetail: unnarrated });
    renderProjectPage(<StoryboardPage />, 'storyboard');

    expect(await screen.findByText('Blocked — prerequisite required')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Complete the narration first.');
  });
});

describe('Claims page', () => {
  it('lists the verified claims with their verdicts', async () => {
    serveProject({
      ...ready,
      claims: [
        claim({ claim_text: 'A network connects computers.', status: 'supported' }),
        claim({
          id: '00000000-0000-4000-8000-000000000071',
          claim_text: 'All networks are wireless.',
          status: 'contradicted',
        }),
      ],
    });
    renderProjectPage(<ClaimsPage />, 'claims');

    expect(await screen.findByRole('heading', { name: 'Verified claims' })).toBeInTheDocument();
    expect(screen.getByText('A network connects computers.')).toBeInTheDocument();
    expect(screen.getByText('All networks are wireless.')).toBeInTheDocument();
    expect(screen.getByText('Supported')).toBeInTheDocument();
    expect(screen.getByText('Contradicted')).toBeInTheDocument();
  });

  const chunkRow = (id: string, index: number, content: string) => ({
    id,
    source_id: source().id,
    project_id: PROJECT_ID,
    chunk_index: index,
    page_from: 4 + index * 9,
    page_to: 6 + index * 9,
    content,
    token_count: 8,
    created_at: '2026-09-26T10:00:00Z',
    updated_at: '2026-09-26T10:00:00Z',
  });
  const citation = (id: string, chunkId: string, similarity: number) => ({
    id,
    source_chunk_id: chunkId,
    similarity,
    created_at: '2026-09-26T10:00:00Z',
  });

  it('shows the source passages a claim was checked against, best match first', async () => {
    serveProject({
      ...ready,
      claims: [
        claim({
          claim_text: 'The OSI model has seven layers.',
          citations: [citation('c-low', 'chunk-2', 0.74), citation('c-high', 'chunk-1', 0.91)],
        }),
      ],
    });
    server.use(
      http.get(`${base}/sources/${source().id}/chunks`, () =>
        HttpResponse.json({
          items: [
            chunkRow('chunk-1', 0, 'The OSI model is a stack of seven layers.'),
            chunkRow('chunk-2', 1, 'Each layer serves the one above it.'),
          ],
        }),
      ),
    );
    const { container } = renderProjectPage(<ClaimsPage />, 'claims');

    // The best match is shown, with where it is and how close it was; the other waits.
    expect(
      await screen.findByText('The OSI model is a stack of seven layers.'),
    ).toBeInTheDocument();
    expect(screen.getByText('Evidence · 2 source passages')).toBeInTheDocument();
    expect(screen.getByText('Pages 4–6 · 91% match')).toBeInTheDocument();
    expect(screen.queryByText('Each layer serves the one above it.')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Show all 2 passages' }));
    expect(screen.getByText('Each layer serves the one above it.')).toBeInTheDocument();
    expect(screen.getByText('Pages 13–15 · 74% match')).toBeInTheDocument();
    expect(await violationsIn(container)).toEqual([]);
  });

  it('says so when a claim cites nothing, or its passage text cannot be loaded', async () => {
    serveProject({
      ...ready,
      claims: [
        claim({ claim_text: 'All networks are wireless.', status: 'unsupported', citations: [] }),
        claim({
          id: '00000000-0000-4000-8000-000000000071',
          claim_text: 'A network connects computers.',
          citations: [citation('c-1', 'chunk-1', 0.9)],
        }),
      ],
    });
    server.use(
      http.get(`${base}/sources/${source().id}/chunks`, () =>
        HttpResponse.json({ detail: 'nope' }, { status: 500 }),
      ),
    );
    renderProjectPage(<ClaimsPage />, 'claims');

    expect(
      await screen.findByText('No source passage was cited for this claim.'),
    ).toBeInTheDocument();
    expect(
      await screen.findByText('1 source passage cited; the text could not be loaded just now.'),
    ).toBeInTheDocument();
  });
});

/** Structure, names, roles and ARIA (contrast is not measurable in jsdom; see test/a11y.test.tsx). */
async function violationsIn(container: HTMLElement): Promise<string[]> {
  const results = await axe.run(container, {
    rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
  });
  return results.violations.map(
    (violation) =>
      `${violation.id}: ${violation.help} -> ${violation.nodes.map((node) => node.target.join(' ')).join(' | ')}`,
  );
}

describe('accessibility of the pipeline pages', () => {
  it('the stepper with every status, and the video, narration, storyboard and claims pages', async () => {
    serveProject(everything);
    serveMedia();
    const video = renderProjectPage(<VideoPage />, 'video');
    await screen.findByLabelText('Your video', { selector: 'video' });
    expect(await violationsIn(video.container)).toEqual([]);
    video.unmount();

    const narration = renderProjectPage(<NarrationPage />, 'narration');
    await screen.findByRole('link', { name: /download audio/i });
    expect(await violationsIn(narration.container)).toEqual([]);
    narration.unmount();

    const storyboard = renderProjectPage(<StoryboardPage />, 'storyboard');
    await screen.findByText('Recap');
    expect(await violationsIn(storyboard.container)).toEqual([]);
    storyboard.unmount();

    const claims = renderProjectPage(<ClaimsPage />, 'claims');
    await screen.findByRole('heading', { name: 'Verified claims' });
    expect(await violationsIn(claims.container)).toEqual([]);
    claims.unmount();

    // A blocked page, with its warning alert and the link to what it is waiting for.
    const unnarrated = scriptDetail({ status: 'approved', measured_duration_ms: null });
    serveProject({ ...ready, scripts: [unnarrated], scriptDetail: unnarrated });
    const blocked = renderProjectPage(<VideoPage />, 'video');
    await screen.findByRole('link', { name: /go to storyboard/i });
    expect(await violationsIn(blocked.container)).toEqual([]);
    // Five page renders and five axe audits in one case: well over vitest's 5 s default on a busy machine.
  }, 30_000);
});
