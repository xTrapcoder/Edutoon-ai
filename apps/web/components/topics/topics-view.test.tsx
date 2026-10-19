import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { claim, job, PROJECT_ID, source, topic } from '@/test/fixtures';
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

  it('polls a running extraction and moves on by itself when the job finishes', async () => {
    // Regression for "stuck on Extracting": the spinner must end on its own once the job does.
    let polls = 0;
    serveProject({ sources: [source()] });
    server.use(
      http.get(`${base}/jobs`, () => {
        polls += 1;
        const status = polls < 2 ? 'running' : 'succeeded';
        return HttpResponse.json({
          items: [job({ status, started_at: new Date().toISOString() })],
          next_cursor: null,
        });
      }),
      http.get(`${base}/topics`, () =>
        HttpResponse.json({
          items: polls < 2 ? [] : [topic({ title: 'Photosynthesis basics', status: 'proposed' })],
          next_cursor: null,
        }),
      ),
    );

    renderProjectPage(<TopicsPage />, 'topics');
    expect(await screen.findByRole('heading', { name: /extracting topics/i })).toBeInTheDocument();

    const found = await screen.findAllByText('Photosynthesis basics', {}, { timeout: 10_000 });
    expect(found.length).toBeGreaterThan(0);
    expect(screen.queryByRole('heading', { name: /extracting topics/i })).not.toBeInTheDocument();
    expect(polls).toBeGreaterThanOrEqual(2);
  }, 20_000);

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

describe('Claim extraction', () => {
  it('offers to verify claims once a topic is accepted, and starts it with an idempotency key', async () => {
    let started = false;
    serveProject({
      sources: [source()],
      topics: [topic({ status: 'accepted' })],
      jobs: [job()],
      claims: [],
    });
    server.use(
      http.post(`${base}/claim-verifications/extraction`, async ({ request }) => {
        started = true;
        expect(request.headers.get('idempotency-key')).toBeTruthy();
        expect(await request.json()).toEqual({ replace: false });
        return HttpResponse.json(job({ kind: 'extract_claims', status: 'queued' }), {
          status: 202,
        });
      }),
      http.get(`${base}/jobs`, () =>
        HttpResponse.json({
          items: started ? [job({ kind: 'extract_claims', status: 'queued' }), job()] : [job()],
          next_cursor: null,
        }),
      ),
    );

    renderProjectPage(<TopicsPage />, 'topics');
    await userEvent.click(await screen.findByRole('button', { name: 'Extract & verify claims' }));

    expect(
      await screen.findByRole('heading', { name: /verifying your topics. claims/i }),
    ).toBeInTheDocument();
    expect(started).toBe(true);
  });

  it('has no extraction action when no topic has been accepted yet', async () => {
    serveProject({
      sources: [source()],
      topics: [topic({ status: 'proposed' })],
      jobs: [job()],
      claims: [],
    });

    renderProjectPage(<TopicsPage />, 'topics');

    await screen.findByRole('heading', { name: 'Review your topics' });
    expect(
      screen.queryByRole('button', { name: /extract & verify claims/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/accept at least one topic to verify claims/i)).toBeInTheDocument();
  });

  it('shows real progress while claim extraction is running', async () => {
    serveProject({
      sources: [source()],
      topics: [topic({ status: 'accepted' })],
      jobs: [job(), job({ kind: 'extract_claims', status: 'running' })],
      claims: [],
    });

    renderProjectPage(<TopicsPage />, 'topics');

    expect(
      await screen.findByRole('heading', { name: /verifying your topics. claims/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /extract & verify claims/i }),
    ).not.toBeInTheDocument();
  });

  it('summarises a finished run in plain language', async () => {
    serveProject({
      sources: [source()],
      topics: [topic({ status: 'accepted' })],
      jobs: [
        job(),
        job({
          kind: 'extract_claims',
          status: 'succeeded',
          result: {
            topics_eligible: 1,
            topics_processed: 1,
            topics_skipped_already_extracted: 0,
            topics_failed: 0,
            candidates_generated: 3,
            claims_verified: 3,
            claims_by_status: { supported: 2, unsupported: 1 },
            failures: [],
            replace: false,
          },
        }),
      ],
      claims: [
        claim({ id: 'c1', status: 'supported' }),
        claim({ id: 'c2', status: 'supported' }),
        claim({ id: 'c3', status: 'unsupported' }),
      ],
    });

    renderProjectPage(<TopicsPage />, 'topics');

    expect(await screen.findByText('Claim extraction finished')).toBeInTheDocument();
    expect(screen.getByText(/2 supported, 1 unsupported/)).toBeInTheDocument();
    expect(screen.getAllByText(/2 supported/).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Extract & verify claims' })).toBeEnabled();
  });

  it('tells the person a failed extraction failed, in words for them, and offers to try again', async () => {
    let attempts = 0;
    serveProject({
      sources: [source()],
      topics: [topic({ status: 'accepted' })],
      jobs: [
        job(),
        job({
          kind: 'extract_claims',
          status: 'failed',
          error:
            'Traceback (most recent call last): File "worker.py", line 12\nanthropic.APIError: 500',
        }),
      ],
      claims: [],
    });
    server.use(
      http.post(`${base}/claim-verifications/extraction`, () => {
        attempts += 1;
        return HttpResponse.json(job({ kind: 'extract_claims', status: 'queued' }), {
          status: 202,
        });
      }),
    );

    renderProjectPage(<TopicsPage />, 'topics');

    expect(await screen.findByText(/last attempt didn.t finish/i)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Traceback|worker\.py|anthropic/);
    const retry = screen.getByRole('button', { name: 'Try again' });
    expect(retry).toBeEnabled();

    await userEvent.click(retry);
    await waitFor(() => expect(attempts).toBe(1));
  });

  it('shows an API error without a raw error, and keeps the person able to retry', async () => {
    serveProject({
      sources: [source()],
      topics: [topic({ status: 'accepted' })],
      jobs: [job()],
      claims: [],
    });
    server.use(
      http.post(`${base}/claim-verifications/extraction`, () =>
        HttpResponse.json(
          { error: { code: 'internal_error', message: 'An unexpected error occurred.' } },
          { status: 500 },
        ),
      ),
    );

    renderProjectPage(<TopicsPage />, 'topics');
    await userEvent.click(await screen.findByRole('button', { name: 'Extract & verify claims' }));

    expect(await screen.findByText(/something went wrong on our side/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Extract & verify claims' })).toBeEnabled();
  });
});
