import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { project, PROJECT_ID } from '@/test/fixtures';
import { renderWithClient } from '@/test/render';
import { API, server } from '@/test/server';
import { ProjectsView } from './projects-view';

describe('Projects list', () => {
  it('shows a plain-language error, never the raw one, and recovers on retry', async () => {
    let healthy = false;
    server.use(
      http.get(`${API}/v1/projects`, () =>
        healthy
          ? HttpResponse.json({
              items: [project({ title: 'Computer Networks' })],
              next_cursor: null,
            })
          : HttpResponse.json(
              {
                error: {
                  code: 'internal_error',
                  message: 'asyncpg.exceptions.ConnectionDoesNotExistError at db.py line 44',
                },
              },
              { status: 500 },
            ),
      ),
      http.get(`${API}/v1/projects/${PROJECT_ID}/jobs`, () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
      http.get(`${API}/v1/projects/${PROJECT_ID}/scripts`, () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
    );

    renderWithClient(<ProjectsView />);

    expect(await screen.findByText('Something went wrong on our side')).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/asyncpg|db\.py|Connection/);

    healthy = true;
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(await screen.findByRole('link', { name: /Computer Networks/ })).toBeInTheDocument();
  });

  it('greets a person with no projects and points them at the first step', async () => {
    server.use(
      http.get(`${API}/v1/projects`, () => HttpResponse.json({ items: [], next_cursor: null })),
    );

    renderWithClient(<ProjectsView />);

    expect(await screen.findByRole('heading', { name: 'No projects yet' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Create your first project' })).toHaveAttribute(
      'href',
      '/projects/new',
    );
  });
});
