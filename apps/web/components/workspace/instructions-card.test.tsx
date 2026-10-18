import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { PROJECT_ID, project } from '@/test/fixtures';
import { renderProjectPage, serveProject } from '@/test/render';
import { API, server } from '@/test/server';
import { InstructionsCard } from './instructions-card';

vi.mock('@/lib/scope', () => ({
  // These tests exercise the dormant PowerPoint mode too, so they run with every mode offered;
  // scope.test.tsx holds the real defaults.
  OFFERED_MODES: ['pdf', 'pptx', 'text', 'both', 'instructions', 'scan'],
  INSTRUCTIONS_OFFERED: true,
}));

const withNotes = project({
  instructions: 'Make it suitable for a beginner. Show photos of leaves.',
  instructions_notes: [
    { kind: 'applied', text: 'Audience: written for complete beginners.' },
    { kind: 'unsupported', text: 'Images, photographs and footage are not generated.' },
  ],
});

describe('Instructions card', () => {
  it('says what became of each part: applied, guidance only, or not possible', async () => {
    serveProject({ project: withNotes });
    renderProjectPage(<InstructionsCard />);

    const notes = await screen.findByRole('list', {
      name: 'What happened to each part of your instructions',
    });
    const [applied, unsupported] = within(notes).getAllByRole('listitem');
    expect(applied).toHaveTextContent('Applied');
    expect(applied).toHaveTextContent('written for complete beginners');
    expect(unsupported).toHaveTextContent("Can't be done");
    expect(unsupported).toHaveTextContent('not generated');
  });

  it('invites instructions when there are none', async () => {
    serveProject({ project: project() });
    renderProjectPage(<InstructionsCard />);

    expect(await screen.findByText(/No instructions\./)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add' })).toBeInTheDocument();
  });

  it('saves changed instructions, and clears them when emptied', async () => {
    serveProject({ project: withNotes });
    const bodies: unknown[] = [];
    server.use(
      http.patch(`${API}/v1/projects/${PROJECT_ID}`, async ({ request }) => {
        const body = (await request.json()) as { instructions: string | null };
        bodies.push(body);
        return HttpResponse.json(project({ instructions: body.instructions }));
      }),
    );
    renderProjectPage(<InstructionsCard />);

    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    const box = screen.getByRole('textbox', { name: /Instructions/ });
    await userEvent.clear(box);
    await userEvent.type(box, 'Focus on chapter 2.');
    await userEvent.click(screen.getByRole('button', { name: 'Save instructions' }));
    await screen.findByRole('button', { name: /Edit|Add/ });

    await userEvent.click(screen.getByRole('button', { name: /Edit|Add/ }));
    await userEvent.clear(screen.getByRole('textbox', { name: /Instructions/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save instructions' }));

    await screen.findByText(/No instructions\./);
    expect(bodies).toEqual([{ instructions: 'Focus on chapter 2.' }, { instructions: null }]);
  });

  it('refuses over-long instructions before sending, naming the limit', async () => {
    serveProject({ project: project() });
    renderProjectPage(<InstructionsCard />);

    await userEvent.click(await screen.findByRole('button', { name: 'Add' }));
    const box = screen.getByRole('textbox', { name: /Instructions/ });
    await userEvent.click(box);
    await userEvent.paste('x'.repeat(2001));

    expect(
      await screen.findByText(/Keep the instructions under 2,000 characters/),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save instructions' })).toBeDisabled();
  });

  it('is not offered for a PowerPoint deck, which no model writes', async () => {
    serveProject({ project: project({ source_type: 'pptx' }) });
    const { container } = renderProjectPage(<InstructionsCard />);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(container).not.toHaveTextContent('Instructions');
  });
});
