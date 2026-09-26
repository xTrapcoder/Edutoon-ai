import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { PROJECT_ID, project, topic } from '@/test/fixtures';
import { router } from '@/test/navigation';
import { renderWithClient } from '@/test/render';
import { API, server } from '@/test/server';
import { CreateProjectWizard } from './create-project-wizard';

describe('Create project wizard', () => {
  it('asks for what is missing before moving on, and says what', async () => {
    renderWithClient(<CreateProjectWizard />);

    await userEvent.click(screen.getByRole('button', { name: 'Next' }));

    expect(await screen.findByText('Give your project a name.')).toBeInTheDocument();
    expect(screen.getByText('Choose a PDF to upload.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Add your source material' })).toBeInTheDocument();
  });

  it('creates a keywords project with the chosen settings, then saves the keywords', async () => {
    const calls: { path: string; key: string | null; body: unknown }[] = [];
    server.use(
      http.post(`${API}/v1/projects`, async ({ request }) => {
        const body = await request.json();
        calls.push({ path: 'project', key: request.headers.get('idempotency-key'), body });
        return HttpResponse.json(project({ source_type: 'topic', title: 'Digital Electronics' }), {
          status: 201,
        });
      }),
      http.post(`${API}/v1/projects/${PROJECT_ID}/topics`, async ({ request }) => {
        const body = await request.json();
        calls.push({ path: 'keywords', key: request.headers.get('idempotency-key'), body });
        return HttpResponse.json({ items: [topic({ origin: 'user' })] }, { status: 201 });
      }),
    );

    renderWithClient(<CreateProjectWizard />);

    // Step 1: source.
    await userEvent.type(screen.getByLabelText(/project title/i), 'Digital Electronics');
    await userEvent.click(screen.getByRole('radio', { name: /^Keywords/ }));
    await userEvent.type(screen.getByLabelText('Add a keyword'), 'Logic gates, Flip-flops{Enter}');
    expect(screen.getByRole('list', { name: 'Keywords added' })).toHaveTextContent('Logic gates');
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));

    // Step 2: settings, defaulting to en-IN.
    expect(
      await screen.findByRole('heading', { name: 'Configure generation settings' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /English \(India\)/ })).toBeChecked();
    await userEvent.click(screen.getByRole('radio', { name: /Vertical/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));

    // Step 3: review, then create.
    expect(await screen.findByRole('heading', { name: 'Review and create' })).toBeInTheDocument();
    expect(screen.getByText('Digital Electronics')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Create project' }));

    await waitFor(() => expect(router.push).toHaveBeenCalledWith(`/projects/${PROJECT_ID}/topics`));
    expect(calls.map((call) => call.path)).toEqual(['project', 'keywords']);
    expect(calls[0]?.body).toEqual({
      title: 'Digital Electronics',
      source_type: 'topic',
      language: 'en-IN',
      orientation: 'vertical',
      target_duration_ms: 300_000,
    });
    expect(calls[1]?.body).toEqual({ keywords: ['Logic gates', 'Flip-flops'] });
    expect(calls.every((call) => call.key && call.key.length >= 8)).toBe(true);
    expect(calls[0]?.key).not.toBe(calls[1]?.key);
  });

  it('keeps the project and offers a retry when a later step fails, instead of starting over', async () => {
    let keywordAttempts = 0;
    let projectCreations = 0;
    server.use(
      http.post(`${API}/v1/projects`, () => {
        projectCreations += 1;
        return HttpResponse.json(project({ source_type: 'topic' }), { status: 201 });
      }),
      http.post(`${API}/v1/projects/${PROJECT_ID}/topics`, () => {
        keywordAttempts += 1;
        return keywordAttempts === 1
          ? HttpResponse.json(
              { error: { code: 'internal_error', message: 'An unexpected error occurred.' } },
              { status: 500 },
            )
          : HttpResponse.json({ items: [topic({ origin: 'user' })] }, { status: 201 });
      }),
    );

    renderWithClient(<CreateProjectWizard />);
    await userEvent.type(screen.getByLabelText(/project title/i), 'Digital Electronics');
    await userEvent.click(screen.getByRole('radio', { name: /^Keywords/ }));
    await userEvent.type(screen.getByLabelText('Add a keyword'), 'Logic gates{Enter}');
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Next' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Create project' }));

    expect(
      await screen.findByText(/your project was created, so nothing is lost/i),
    ).toBeInTheDocument();
    expect(router.push).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));

    await waitFor(() => expect(router.push).toHaveBeenCalledWith(`/projects/${PROJECT_ID}/topics`));
    expect(projectCreations).toBe(1);
    expect(keywordAttempts).toBe(2);
  });
});
