import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import {
  job,
  PROJECT_ID,
  project,
  scriptDetail,
  segmentAudio,
  source,
  synthesisStatus,
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

const approvedDetail = scriptDetail({ status: 'approved' });
const [approvedSegment] = approvedDetail.segments;
if (!approvedSegment) throw new Error('fixture must have a segment');

describe('Narration (Phase 8)', () => {
  it('offers narration for an approved script, and shows real progress once requested', async () => {
    let started = false;
    serveProject({ ...ready, scripts: [approvedDetail], scriptDetail: approvedDetail });
    server.use(
      http.post(`${base}/scripts/:id/synthesis`, ({ request }) => {
        started = true;
        expect(request.headers.get('idempotency-key')).toBeTruthy();
        return HttpResponse.json(job({ kind: 'synthesise_voice', status: 'queued' }), {
          status: 202,
        });
      }),
      http.get(`${base}/scripts/:id/synthesis`, () =>
        HttpResponse.json(
          synthesisStatus({
            script: approvedDetail,
            job: started
              ? job({
                  kind: 'synthesise_voice',
                  status: 'running',
                  payload: {
                    progress: {
                      stage: 'synthesising_segment',
                      segment_index: 0,
                      segment_count: 1,
                    },
                  },
                })
              : null,
            segments: approvedDetail.segments.map((segment) => ({
              id: segment.id,
              segment_index: segment.segment_index,
              estimated_duration_ms: segment.estimated_duration_ms,
              audio: null,
            })),
          }),
        ),
      ),
    );

    renderProjectPage(<ScriptPage />, 'script');

    await userEvent.click(await screen.findByRole('button', { name: 'Narrate this script' }));

    expect(await screen.findByText(/narrating section 1 of 1/i)).toBeInTheDocument();
    // Only what is really known: no invented percentage text is asserted here, the progress
    // bar itself carries the determinate value via aria-valuenow.
    expect(screen.getByRole('progressbar', { name: 'Narration progress' })).toBeInTheDocument();
  });

  it('shows a playable, measured narration once a segment is ready', async () => {
    const audio = segmentAudio({ measured_duration_ms: 62_000 });
    serveProject({
      ...ready,
      scripts: [approvedDetail],
      scriptDetail: approvedDetail,
      synthesisStatus: synthesisStatus({
        script: { ...approvedDetail, measured_duration_ms: 62_000 },
        job: job({
          kind: 'synthesise_voice',
          status: 'succeeded',
          result: { ready_segment_count: 1, segment_count: 1, failed_segment_ids: [] },
        }),
        segments: [
          {
            id: approvedSegment.id,
            segment_index: 0,
            estimated_duration_ms: approvedSegment.estimated_duration_ms,
            audio,
          },
        ],
      }),
    });
    server.use(
      http.get(
        `${base}/scripts/:id/segments/:segmentId/audio`,
        () =>
          new HttpResponse(new Uint8Array(8), {
            headers: { 'Content-Type': 'audio/wav' },
          }),
      ),
    );

    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByText('All sections narrated')).toBeInTheDocument();
    expect(screen.getByText(/1 min 02 s narrated/i)).toBeInTheDocument();
    await waitFor(() => {
      const player = document.querySelector('audio');
      expect(player).not.toBeNull();
      expect(player?.getAttribute('src')).toMatch(/^blob:/);
    });
  });

  it("labels an en-IN project's narration as a temporary British-English substitute", async () => {
    serveProject({
      project: project({ language: 'en-IN' }),
      ...ready,
      scripts: [approvedDetail],
      scriptDetail: approvedDetail,
    });

    renderProjectPage(<ScriptPage />, 'script');

    expect(
      await screen.findByText(/genuine Indian-English voice isn.t available/i),
    ).toBeInTheDocument();
  });

  it('never shows the temporary-substitute note for an en-GB project', async () => {
    serveProject({
      project: project({ language: 'en-GB' }),
      ...ready,
      scripts: [approvedDetail],
      scriptDetail: approvedDetail,
    });

    renderProjectPage(<ScriptPage />, 'script');

    await screen.findByRole('button', { name: 'Narrate this script' });
    expect(screen.queryByText(/genuine Indian-English voice/i)).toBeNull();
  });

  it('shows partial-failure state and offers to retry only what failed', async () => {
    const twoSegments = scriptDetail({
      status: 'approved',
      segments: [
        approvedSegment,
        {
          ...approvedSegment,
          id: '00000000-0000-4000-8000-000000000051',
          segment_index: 1,
        },
      ],
    });
    const [firstOfTwo, secondOfTwo] = twoSegments.segments;
    if (!firstOfTwo || !secondOfTwo) throw new Error('fixture must have two segments');
    serveProject({
      ...ready,
      scripts: [twoSegments],
      scriptDetail: twoSegments,
      synthesisStatus: synthesisStatus({
        script: twoSegments,
        job: job({
          kind: 'synthesise_voice',
          status: 'succeeded',
          result: {
            ready_segment_count: 1,
            segment_count: 2,
            failed_segment_ids: [secondOfTwo.id],
          },
        }),
        segments: [
          {
            id: firstOfTwo.id,
            segment_index: 0,
            estimated_duration_ms: firstOfTwo.estimated_duration_ms,
            audio: segmentAudio(),
          },
          {
            id: secondOfTwo.id,
            segment_index: 1,
            estimated_duration_ms: secondOfTwo.estimated_duration_ms,
            audio: segmentAudio({ status: 'failed', error: "This step didn't finish." }),
          },
        ],
      }),
    });

    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByText(/1 section.* couldn.t be narrated/i)).toBeInTheDocument();
    expect(
      await screen.findByRole('button', { name: 'Retry remaining sections' }),
    ).toBeInTheDocument();
  });

  it('narration is refused (never offered) for a script that is not yet approved', async () => {
    const draftDetail = scriptDetail({ status: 'draft' });
    serveProject({ ...ready, scripts: [draftDetail], scriptDetail: draftDetail });

    renderProjectPage(<ScriptPage />, 'script');

    await screen.findByRole('navigation', { name: 'Script sections' });
    expect(screen.queryByRole('button', { name: /narrate/i })).toBeNull();
  });
});
