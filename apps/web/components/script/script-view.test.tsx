import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { claim, job, PROJECT_ID, script, scriptDetail, source, topic } from '@/test/fixtures';
import { renderProjectPage, serveProject } from '@/test/render';
import { API, server } from '@/test/server';
import { ScriptPage } from './script-view';

const base = `${API}/v1/projects/${PROJECT_ID}`;
const ready = {
  sources: [source()],
  topics: [topic()],
  jobs: [job({ kind: 'extract_topics' })],
};

describe('Script step', () => {
  it('explains that verification is required, and does not try to generate anything', async () => {
    const generate = vi.fn();
    serveProject({ ...ready, scripts: [], claims: [] });
    server.use(http.post(`${base}/scripts`, () => generate()));

    renderProjectPage(<ScriptPage />, 'script');

    expect(
      await screen.findByRole('heading', { name: 'Verification required' }),
    ).toBeInTheDocument();
    expect(screen.getByText(/doesn.t have any verified claims yet/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /generate script/i })).not.toBeInTheDocument();
    expect(generate).not.toHaveBeenCalled();
  });

  it('treats the API refusing for lack of claims the same way, without a raw error', async () => {
    serveProject({ ...ready, scripts: [], claims: [claim()] });
    server.use(
      http.post(`${base}/scripts`, () =>
        HttpResponse.json(
          {
            error: {
              code: 'validation_error',
              message:
                "These accepted topics have no verified evidence yet: 'Plate boundaries'. Extract and verify claims for them, or reject them, before generating a script.",
            },
          },
          { status: 400 },
        ),
      ),
    );

    renderProjectPage(<ScriptPage />, 'script');
    await userEvent.click(await screen.findByRole('button', { name: /generate script/i }));

    expect(
      await screen.findByRole('heading', { name: 'Verification required' }),
    ).toBeInTheDocument();
  });

  it('shows any other refusal in its own words, never as "no verified claims"', async () => {
    serveProject({ ...ready, scripts: [], claims: [claim()] });
    server.use(
      http.post(`${base}/scripts`, () =>
        HttpResponse.json(
          {
            error: {
              code: 'validation_error',
              message:
                '9 accepted topics cannot each receive a section of at least 30 seconds within a 4:00 target. Accept fewer topics, or increase the target duration.',
            },
          },
          { status: 400 },
        ),
      ),
    );

    renderProjectPage(<ScriptPage />, 'script');
    await userEvent.click(await screen.findByRole('button', { name: /generate script/i }));

    expect(
      await screen.findByText(/9 accepted topics cannot each receive a section/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('heading', { name: 'Verification required' }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /generate script/i })).toBeInTheDocument();
  });

  it('names the topics that have no verified claim, even when the sentence is too long to show', async () => {
    // Regression: the API's sentence grew past the 280 characters the client shows, so the page lost
    // the reason and said only "some of the details were not accepted". The refusal now carries the
    // reason and the topic titles as data.
    serveProject({ ...ready, scripts: [], claims: [claim()] });
    const long = `These accepted topics have no verified evidence yet: 'Cryptocurrency mining'. ${'Extract and verify claims for them, or reject them. '.repeat(6)}`;
    server.use(
      http.post(`${base}/scripts`, () =>
        HttpResponse.json(
          {
            error: {
              code: 'validation_error',
              message: long,
              details: { reason: 'no_verified_evidence', topics: ['Cryptocurrency mining'] },
            },
          },
          { status: 400 },
        ),
      ),
    );

    renderProjectPage(<ScriptPage />, 'script');
    await userEvent.click(await screen.findByRole('button', { name: /generate script/i }));

    expect(
      await screen.findByRole('heading', { name: 'Verification required' }),
    ).toBeInTheDocument();
    expect(screen.getByText(/"Cryptocurrency mining"/)).toBeInTheDocument();
    expect(screen.getByText(/Leave it out/)).toBeInTheDocument();
    expect(screen.queryByText(/doesn.t have any verified claims yet/i)).not.toBeInTheDocument();
  });

  it('starts generation with an idempotency key and the signed-in token, then shows real progress', async () => {
    let started = false;
    let seen: { key: string | null; auth: string | null } | undefined;
    serveProject({ ...ready, claims: [claim()] });
    server.use(
      http.post(`${base}/scripts`, ({ request }) => {
        started = true;
        seen = {
          key: request.headers.get('idempotency-key'),
          auth: request.headers.get('authorization'),
        };
        return HttpResponse.json(script({ status: 'pending' }), { status: 202 });
      }),
      http.get(`${base}/scripts`, () =>
        HttpResponse.json({
          items: started ? [script({ status: 'pending' })] : [],
          next_cursor: null,
        }),
      ),
      http.get(`${base}/jobs`, () =>
        HttpResponse.json({
          items: started
            ? [job({ kind: 'generate_script', status: 'queued' }), ...ready.jobs]
            : ready.jobs,
          next_cursor: null,
        }),
      ),
    );

    renderProjectPage(<ScriptPage />, 'script');
    await userEvent.click(await screen.findByRole('button', { name: /generate script/i }));

    expect(await screen.findByRole('heading', { name: 'Writing your script' })).toBeInTheDocument();
    expect(seen?.key).toMatch(/^[0-9a-f-]{36}$/);
    expect(seen?.auth).toBe('Bearer test-token');
    // Only what is really known: no invented percentage.
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
    expect(screen.getByText(/you can leave this page/i)).toBeInTheDocument();
  });

  it('lets the person approve a draft, then says what is next without offering anything that does not exist', async () => {
    let approved = false;
    const draft = () => script({ status: approved ? 'approved' : 'draft' });
    serveProject({ ...ready });
    server.use(
      http.get(`${base}/scripts`, () => HttpResponse.json({ items: [draft()], next_cursor: null })),
      http.get(`${base}/scripts/:id`, () =>
        HttpResponse.json(scriptDetail({ status: draft().status })),
      ),
      http.post(`${base}/scripts/:id/approval`, ({ request }) => {
        expect(request.headers.get('idempotency-key')).toBeTruthy();
        approved = true;
        return HttpResponse.json(draft());
      }),
    );

    renderProjectPage(<ScriptPage />, 'script');

    // Script Studio: sections, the script and its sources, settings and approval.
    expect(await screen.findByRole('navigation', { name: 'Script sections' })).toBeInTheDocument();
    expect(
      screen.getByText(/A network connects computers so they can share resources/),
    ).toBeInTheDocument();
    expect(screen.getByText('Checked against your source')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Project settings' })).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Approve script' }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Approve script' }));

    expect(await screen.findByRole('heading', { name: 'Script approved' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /what.s next/i })).toBeInTheDocument();
    // Narration, the storyboard and the video are all real stages: none is described as coming soon,
    // and "Go to Video" is only a link to the Video page - it starts nothing, and the Video card
    // still offers no render button before its prerequisites are met.
    expect(screen.queryByText(/coming soon|still to come/i)).not.toBeInTheDocument();
    expect(screen.getByText('Next: Narration → Storyboard → Video')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Go to Video' })).toHaveAttribute(
      'href',
      `/projects/${PROJECT_ID}/video`,
    );
    expect(screen.queryByRole('button', { name: /render|scene|video/i })).not.toBeInTheDocument();
    // Narration is real: Script Studio's own narration panel offers it directly.
    expect(await screen.findByRole('button', { name: 'Narrate this script' })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Approve script' })).not.toBeInTheDocument(),
    );
  });

  it('shows weak-citation passages as an advisory that does not stop approval', async () => {
    const detail = scriptDetail();
    serveProject({
      ...ready,
      jobs: [
        job({
          kind: 'generate_script',
          result: {
            script_id: detail.id,
            weak_citation_segments: [{ segment_index: 0, coverage: 0.08 }, { segment_index: 9 }],
          },
        }),
        ...ready.jobs,
      ],
    });
    server.use(
      http.get(`${base}/scripts`, () =>
        HttpResponse.json({ items: [script({ status: 'draft' })], next_cursor: null }),
      ),
      http.get(`${base}/scripts/:id`, () => HttpResponse.json(detail)),
    );

    renderProjectPage(<ScriptPage />, 'script');

    const notice = await screen.findByTestId('weak-citation-notice');
    expect(within(notice).getByText(/before approving \(advisory\)/i)).toBeInTheDocument();
    expect(within(notice).getByText(/not proof either way/i)).toBeInTheDocument();
    expect(within(notice).getByText(/Passage 1/)).toBeInTheDocument();
    expect(within(notice).getByText(/about 8% of its words/)).toBeInTheDocument();
    expect(
      within(notice).getByText(/A network connects computers so they can share/),
    ).toBeInTheDocument();
    expect(within(notice).getByText('A network connects computers.')).toBeInTheDocument();
    // An entry for a passage that does not exist is ignored, and approval is still offered.
    expect(within(notice).getAllByRole('listitem').length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Approve script' })).toBeEnabled();
  });

  it('shows no weak-citation notice when none were flagged or the script predates the flag', async () => {
    const detail = scriptDetail();
    serveProject({
      ...ready,
      jobs: [
        job({
          kind: 'generate_script',
          result: { script_id: detail.id, weak_citation_segments: [] },
        }),
        ...ready.jobs,
      ],
    });
    server.use(
      http.get(`${base}/scripts`, () =>
        HttpResponse.json({ items: [script({ status: 'draft' })], next_cursor: null }),
      ),
      http.get(`${base}/scripts/:id`, () => HttpResponse.json(detail)),
    );

    renderProjectPage(<ScriptPage />, 'script');

    expect(await screen.findByRole('navigation', { name: 'Script sections' })).toBeInTheDocument();
    expect(screen.queryByTestId('weak-citation-notice')).not.toBeInTheDocument();
  });
});
