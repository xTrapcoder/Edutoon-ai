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
                'This project has no supported claims yet - verify some source material before generating a script.',
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
    // Rendering is still described, never offered - only 1 "Coming soon" stage now that
    // narration (Phase 8) and the storyboard (Phase 9) are both real.
    expect(screen.getAllByText('Coming soon')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: /render|scene|video/i })).not.toBeInTheDocument();
    // Narration is real: Script Studio's own narration panel offers it directly.
    expect(await screen.findByRole('button', { name: 'Narrate this script' })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Approve script' })).not.toBeInTheDocument(),
    );
  });
});
