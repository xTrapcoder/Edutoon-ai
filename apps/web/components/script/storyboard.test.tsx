import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import {
  job,
  PROJECT_ID,
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

describe('Storyboard (Phase 9)', () => {
  it('is offered only once narration is fully measured', async () => {
    const notYetMeasured = scriptDetail({ status: 'approved', measured_duration_ms: null });
    serveProject({ ...ready, scripts: [notYetMeasured], scriptDetail: notYetMeasured });

    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByText('Storyboard')).toBeInTheDocument();
    expect(screen.getByText(/finish narrating this script first/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /generate storyboard/i })).toBeNull();
  });

  it('shows the estimated call count and triggers generation, then shows real progress', async () => {
    let started = false;
    serveProject({ ...ready, scripts: [approvedDetail], scriptDetail: approvedDetail });
    server.use(
      http.post(`${base}/scripts/:id/storyboard`, ({ request }) => {
        started = true;
        expect(request.headers.get('idempotency-key')).toBeTruthy();
        return HttpResponse.json(
          {
            job: job({ kind: 'generate_storyboard', status: 'queued' }),
            estimated_provider_calls: 1,
          },
          { status: 202 },
        );
      }),
      http.get(`${base}/scripts/:id/storyboard`, () =>
        HttpResponse.json(
          storyboardStatus({
            script: approvedDetail,
            job: started
              ? job({
                  kind: 'generate_storyboard',
                  status: 'running',
                  payload: {
                    progress: { stage: 'processing_segment', segment_index: 0, segment_count: 1 },
                  },
                })
              : null,
            segments: [
              segmentStoryboard({
                id: approvedSegment.id,
                segment_index: 0,
                audio_measured_duration_ms: 60_000,
              }),
            ],
            not_ready_segment_ids: [approvedSegment.id],
          }),
        ),
      ),
    );

    renderProjectPage(<ScriptPage />, 'script');

    expect(
      await screen.findByText(
        (_, element) => /estimated ai calls:\s*1/i.test(element?.textContent ?? ''),
        { selector: 'p' },
      ),
    ).toBeInTheDocument();
    await userEvent.click(await screen.findByRole('button', { name: 'Generate storyboard' }));

    expect(await screen.findByText(/storyboarding section 1 of 1/i)).toBeInTheDocument();
    expect(screen.getByRole('progressbar', { name: 'Storyboard progress' })).toBeInTheDocument();
  });

  it('shows a complete storyboard with its scene timeline', async () => {
    serveProject({
      ...ready,
      scripts: [approvedDetail],
      scriptDetail: approvedDetail,
      storyboardStatus: storyboardStatus({
        script: approvedDetail,
        job: job({
          kind: 'generate_storyboard',
          status: 'succeeded',
          result: {
            ready_segment_count: 1,
            segment_count: 1,
            failed_segment_ids: [],
            outcome: 'complete',
          },
        }),
        segments: [
          segmentStoryboard({
            id: approvedSegment.id,
            segment_index: 0,
            is_ready: true,
            scenes: [
              scene({
                template: 'bullet_points',
                content: { title: 'Recap', bullets: ['Networks share resources'] },
              }),
            ],
          }),
        ],
        ready_segment_count: 1,
        not_ready_segment_ids: [],
        is_complete: true,
        actual_provider_calls: 1,
      }),
    });

    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByText('Storyboard: Complete')).toBeInTheDocument();
    expect(screen.getByText('Recap')).toBeInTheDocument();
    expect(screen.getByText('Networks share resources')).toBeInTheDocument();
    expect(screen.getByText('Bullet points')).toBeInTheDocument();
  });

  it('shows an illustration slot as pending, never as a real image', async () => {
    serveProject({
      ...ready,
      scripts: [approvedDetail],
      scriptDetail: approvedDetail,
      storyboardStatus: storyboardStatus({
        script: approvedDetail,
        segments: [
          segmentStoryboard({
            id: approvedSegment.id,
            is_ready: true,
            scenes: [
              scene({
                template: 'full_bleed_illustration',
                content: { illustration: { description: 'A sunlit server room' } },
              }),
            ],
          }),
        ],
        ready_segment_count: 1,
        not_ready_segment_ids: [],
        is_complete: true,
      }),
    });

    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByText('Illustration pending')).toBeInTheDocument();
    expect(screen.getByText(/A sunlit server room/i)).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
  });

  it('shows one banner, and no per-row placeholders, while timings can still move', async () => {
    serveProject({
      ...ready,
      scripts: [approvedDetail],
      scriptDetail: approvedDetail,
      storyboardStatus: storyboardStatus({
        script: approvedDetail,
        timings_authoritative: false,
        segments: [
          segmentStoryboard({
            id: approvedSegment.id,
            is_ready: true,
            scenes: [
              scene({ start_ms: 14_000, duration_ms: 9_000 }),
              scene({
                id: '00000000-0000-4000-8000-000000000081',
                scene_index: 1,
                start_ms: 23_000,
              }),
            ],
          }),
        ],
        ready_segment_count: 1,
        not_ready_segment_ids: [],
        is_complete: true,
      }),
    });

    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findAllByText('Timings update when narration finishes.')).toHaveLength(1);
    // The times themselves are still shown as ordinary numbers - no dashes or nulls.
    expect(screen.getByText(/14 s–23 s/)).toBeInTheDocument();
    expect(screen.queryByText('—')).toBeNull();
  });

  it('shows no timings banner when every timing input is final', async () => {
    serveProject({
      ...ready,
      scripts: [approvedDetail],
      scriptDetail: approvedDetail,
      storyboardStatus: storyboardStatus({
        script: approvedDetail,
        timings_authoritative: true,
        segments: [
          segmentStoryboard({ id: approvedSegment.id, is_ready: true, scenes: [scene()] }),
        ],
        ready_segment_count: 1,
        not_ready_segment_ids: [],
        is_complete: true,
      }),
    });

    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByText('Storyboard: Complete')).toBeInTheDocument();
    expect(screen.queryByText('Timings update when narration finishes.')).toBeNull();
  });

  it('does not show the timings banner before any scene exists', async () => {
    serveProject({
      ...ready,
      scripts: [approvedDetail],
      scriptDetail: approvedDetail,
      storyboardStatus: storyboardStatus({
        script: approvedDetail,
        timings_authoritative: false,
        segments: [segmentStoryboard({ id: approvedSegment.id })],
      }),
    });

    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByText('Storyboard')).toBeInTheDocument();
    expect(screen.queryByText('Timings update when narration finishes.')).toBeNull();
  });

  it('shows the reviewer flag as a warning, never a failure', async () => {
    serveProject({
      ...ready,
      scripts: [approvedDetail],
      scriptDetail: approvedDetail,
      storyboardStatus: storyboardStatus({
        script: approvedDetail,
        segments: [
          segmentStoryboard({
            id: approvedSegment.id,
            is_ready: true,
            scenes: [scene({ reviewer_flag: true })],
          }),
        ],
        ready_segment_count: 1,
        not_ready_segment_ids: [],
        is_complete: true,
      }),
    });

    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByText(/reviewer: check this against the source/i)).toBeInTheDocument();
    expect(screen.getByText('Storyboard: Complete')).toBeInTheDocument();
  });

  it('shows incomplete state with a retry action when some sections failed', async () => {
    const twoSegments = scriptDetail({
      status: 'approved',
      measured_duration_ms: 120_000,
      segments: [
        approvedSegment,
        { ...approvedSegment, id: '00000000-0000-4000-8000-000000000052', segment_index: 1 },
      ],
    });
    const [firstOfTwo, secondOfTwo] = twoSegments.segments;
    if (!firstOfTwo || !secondOfTwo) throw new Error('fixture must have two segments');
    serveProject({
      ...ready,
      scripts: [twoSegments],
      scriptDetail: twoSegments,
      storyboardStatus: storyboardStatus({
        script: twoSegments,
        job: job({
          kind: 'generate_storyboard',
          status: 'succeeded',
          result: {
            ready_segment_count: 1,
            segment_count: 2,
            failed_segment_ids: [secondOfTwo.id],
            outcome: 'succeeded_with_failures',
          },
        }),
        segments: [
          segmentStoryboard({
            id: firstOfTwo.id,
            segment_index: 0,
            is_ready: true,
            scenes: [scene()],
          }),
          segmentStoryboard({ id: secondOfTwo.id, segment_index: 1, is_ready: false, scenes: [] }),
        ],
        ready_segment_count: 1,
        not_ready_segment_ids: [secondOfTwo.id],
        is_complete: false,
      }),
    });

    renderProjectPage(<ScriptPage />, 'script');

    expect(
      await screen.findByText(/storyboard: incomplete.*1 of 2 sections need another attempt/i),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole('button', { name: 'Retry failed sections' }),
    ).toBeInTheDocument();
  });

  it('is not offered for a script that is not yet approved', async () => {
    const draftDetail = scriptDetail({ status: 'draft' });
    serveProject({ ...ready, scripts: [draftDetail], scriptDetail: draftDetail });

    renderProjectPage(<ScriptPage />, 'script');

    await screen.findByRole('navigation', { name: 'Script sections' });
    expect(screen.queryByText('Storyboard')).toBeNull();
  });
});
