import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { job, PROJECT_ID, source, topic } from '@/test/fixtures';
import { renderProjectPage, serveProject } from '@/test/render';
import { API, server } from '@/test/server';
import { TopicsPage } from './topics-view';

const base = `${API}/v1/projects/${PROJECT_ID}`;

describe('Topics step', () => {
  it('offers to extract topics once the source has been read, and starts it on request', async () => {
    let started = false;
    serveProject({ sources: [source()] });
    server.use(
      http.post(`${base}/topics/extraction`, async ({ request }) => {
        started = true;
        expect(request.headers.get('idempotency-key')).toBeTruthy();
        expect(await request.json()).toEqual({ replace: false });
        return HttpResponse.json(job({ status: 'queued' }), { status: 202 });
      }),
      http.get(`${base}/jobs`, () =>
        HttpResponse.json({ items: started ? [job({ status: 'queued' })] : [], next_cursor: null }),
      ),
    );

    renderProjectPage(<TopicsPage />, 'topics');
    await userEvent.click(await screen.findByRole('button', { name: 'Extract topics' }));

    expect(await screen.findByRole('heading', { name: /extracting topics/i })).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow');
  });

  it('tells the person a failed extraction failed, in words for them, and never shows raw error text', async () => {
    serveProject({
      sources: [source()],
      jobs: [
        job({
          status: 'failed',
          error:
            'Traceback (most recent call last): File "worker.py", line 88, in run\nanthropic.APIError: 500 {"error": "boom"}',
        }),
      ],
    });

    renderProjectPage(<TopicsPage />, 'topics');

    expect(await screen.findByText(/last attempt didn.t finish/i)).toBeInTheDocument();
    expect(screen.getByText("This step didn't finish. You can try again.")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Traceback|worker\.py|anthropic|boom/);
    expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled();
  });

  it('shows where a topic came from, with the passage, and saves an accept decision', async () => {
    const withEvidence = topic({
      status: 'proposed',
      evidence: [{ source_chunk_id: 'chunk-1', source_id: source().id, page_from: 4, page_to: 6 }],
    });
    let patched: unknown;
    serveProject({ sources: [source()], topics: [withEvidence], jobs: [job()] });
    server.use(
      http.get(`${base}/sources/${source().id}/chunks`, () =>
        HttpResponse.json({
          items: [
            {
              id: 'chunk-1',
              source_id: source().id,
              project_id: PROJECT_ID,
              chunk_index: 0,
              page_from: 4,
              page_to: 6,
              content: 'The OSI model has seven layers.',
              token_count: 7,
              created_at: '2026-09-26T10:00:00Z',
              updated_at: '2026-09-26T10:00:00Z',
            },
          ],
        }),
      ),
      http.patch(`${base}/topics/${withEvidence.id}`, async ({ request }) => {
        patched = await request.json();
        return HttpResponse.json({ ...withEvidence, status: 'accepted' });
      }),
    );

    renderProjectPage(<TopicsPage />, 'topics');

    expect(await screen.findByText('The OSI model has seven layers.')).toBeInTheDocument();
    expect(screen.getAllByText(/Pages 4–6/).length).toBeGreaterThan(0);

    await userEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(patched).toEqual({ status: 'accepted' }));
  });

  it('marks a suggested topic as unverified, so nothing reads as sourced when it is not', async () => {
    serveProject({
      sources: [],
      topics: [topic({ origin: 'suggested', evidence: [] })],
      jobs: [job()],
    });

    renderProjectPage(<TopicsPage />, 'topics');

    expect(await screen.findByText('Suggested, and not verified')).toBeInTheDocument();
  });
});
