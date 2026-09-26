import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { http, HttpResponse } from 'msw';
import type { ReactElement } from 'react';
import { describe, expect, it } from 'vitest';
import { DashboardView } from '@/components/dashboard/dashboard-view';
import { ScriptPage } from '@/components/script/script-view';
import { TopicsPage } from '@/components/topics/topics-view';
import { CreateProjectWizard } from '@/components/wizard/create-project-wizard';
import { job, project, PROJECT_ID, script, scriptDetail, source, topic } from './fixtures';
import { renderProjectPage, renderWithClient, serveProject } from './render';
import { API, server } from './server';

/**
 * Structure, names, roles and ARIA on the screens people spend their time in. Colour contrast is
 * not measurable in jsdom; the palette's pairs are checked separately (see the design tokens).
 */
async function violationsIn(container: HTMLElement): Promise<string[]> {
  const results = await axe.run(container, {
    rules: {
      'color-contrast': { enabled: false },
      // Fragments are rendered without the page's <main> landmark around them.
      region: { enabled: false },
    },
  });
  return results.violations.map(
    (violation) =>
      `${violation.id}: ${violation.help} -> ${violation.nodes.map((node) => node.target.join(' ')).join(' | ')}`,
  );
}

async function expectAccessible(ui: ReactElement, ready: () => Promise<unknown>) {
  const { container } = renderWithClient(ui);
  await ready();
  expect(await violationsIn(container)).toEqual([]);
}

describe('accessibility', () => {
  it('the create-project wizard, on each step', async () => {
    const { container } = renderWithClient(<CreateProjectWizard />);
    expect(await violationsIn(container)).toEqual([]);

    await userEvent.click(screen.getByRole('radio', { name: /^Keywords/ }));
    await userEvent.type(screen.getByLabelText(/project title/i), 'Digital Electronics');
    await userEvent.type(screen.getByLabelText('Add a keyword'), 'Logic gates{Enter}');
    expect(await violationsIn(container)).toEqual([]);

    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByRole('heading', { name: 'Configure generation settings' });
    expect(await violationsIn(container)).toEqual([]);

    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByRole('heading', { name: 'Review and create' });
    expect(await violationsIn(container)).toEqual([]);
  });

  it('the dashboard', async () => {
    server.use(
      http.get(`${API}/v1/projects`, () =>
        HttpResponse.json({ items: [project()], next_cursor: null }),
      ),
      http.get(`${API}/v1/projects/${PROJECT_ID}/jobs`, () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
      http.get(`${API}/v1/projects/${PROJECT_ID}/scripts`, () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
    );
    await expectAccessible(<DashboardView />, () =>
      screen.findByRole('link', { name: /Computer Networks/ }),
    );
  });

  it('the topic review, with its hierarchy and evidence', async () => {
    serveProject({
      sources: [source()],
      jobs: [job()],
      topics: [
        topic({ id: 'a', title: 'Models', status: 'accepted' }),
        topic({ id: 'a1', parent_id: 'a', title: 'OSI', depth: 1 }),
        topic({ id: 'b', title: 'Devices', origin: 'suggested' }),
      ],
    });
    renderProjectPage(<TopicsPage />, 'topics');
    await screen.findByRole('heading', { name: 'Review your topics' });
    expect(await violationsIn(document.body)).toEqual([]);
  });

  it('the verification-required state and the script studio', async () => {
    serveProject({
      sources: [source()],
      topics: [topic()],
      jobs: [job()],
      scripts: [],
      claims: [],
    });
    const first = renderProjectPage(<ScriptPage />, 'script');
    await screen.findByRole('heading', { name: 'Verification required' });
    expect(await violationsIn(first.container)).toEqual([]);
    first.unmount();

    serveProject({
      sources: [source()],
      topics: [topic()],
      jobs: [job()],
      scripts: [script({ status: 'draft' })],
      scriptDetail: scriptDetail(),
    });
    const second = renderProjectPage(<ScriptPage />, 'script');
    await screen.findByRole('navigation', { name: 'Script sections' });
    expect(await violationsIn(second.container)).toEqual([]);
  });
});
