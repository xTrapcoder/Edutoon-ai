import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PROJECT_ID, project, source, topic } from '@/test/fixtures';
import { router } from '@/test/navigation';
import { renderWithClient } from '@/test/render';
import { API, server } from '@/test/server';
import { CreateProjectWizard } from './create-project-wizard';

const PDF = new File([new Uint8Array(1000)], 'electronics.pdf', { type: 'application/pdf' });
const DECK = new File([new Uint8Array(1000)], 'safety_induction.pptx', {
  type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
});

interface Call {
  path: string;
  key: string | null;
  body: unknown;
}
const shared = vi.hoisted(() => ({
  calls: [] as { path: string; key: string | null; body: unknown }[],
}));

// jsdom's File cannot go through Node's native FormData, so the one real multipart request is
// replaced by a recording stand-in; every other request in these tests goes through the real client.
vi.mock('@/lib/api/endpoints', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/endpoints')>()),
  uploadSource: vi.fn(async (_projectId: string, file: File, key: string) => {
    shared.calls.push({ path: 'upload', key, body: { file: file.name } });
    return source();
  }),
}));

beforeEach(() => {
  shared.calls.length = 0;
});

/** Step 1 for a PDF + keywords project: a title, the PDF and a couple of keywords. */
async function fillSource() {
  await userEvent.type(screen.getByLabelText(/project title/i), 'Digital Electronics');
  await userEvent.click(screen.getByRole('radio', { name: /^PDF \+ Keywords/ }));
  await userEvent.upload(screen.getByLabelText(/drag and drop your pdf/i), PDF);
  await userEvent.type(screen.getByLabelText('Add a keyword'), 'Logic gates, Flip-flops{Enter}');
}

describe('Create project wizard', () => {
  it('asks for what is missing before moving on, and says what', async () => {
    renderWithClient(<CreateProjectWizard />);

    await userEvent.click(screen.getByRole('button', { name: 'Next' }));

    expect(await screen.findByText('Give your project a name.')).toBeInTheDocument();
    expect(screen.getByText('Choose a PDF to upload.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Add your source material' })).toBeInTheDocument();
  });

  it('does not offer keyword-only projects, and says why and what to do instead', async () => {
    renderWithClient(<CreateProjectWizard />);

    const keywordsOnly = screen.getByRole('radio', { name: /^Keywords/ });
    expect(keywordsOnly).toBeDisabled();
    expect(keywordsOnly).not.toBeChecked();
    expect(screen.getByRole('radio', { name: /^PDF document/ })).toBeChecked(); // the default
    expect(screen.getByText('Not yet')).toBeInTheDocument();
    expect(screen.getByText(/keyword-only projects are not supported yet/i)).toBeInTheDocument();
    expect(screen.getByText(/upload a pdf or a powerpoint deck/i)).toBeInTheDocument();
  });

  it('never sends a keyword-only project to the API, even if the choice were forced', async () => {
    let creations = 0;
    server.use(
      http.post(`${API}/v1/projects`, () => {
        creations += 1;
        return HttpResponse.json(project({ source_type: 'topic' }), { status: 201 });
      }),
    );
    renderWithClient(<CreateProjectWizard />);

    await userEvent.type(screen.getByLabelText(/project title/i), 'Digital Electronics');
    // The radio is disabled, so the only way in is not to be able to click it.
    await userEvent.click(screen.getByRole('radio', { name: /^Keywords/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));

    expect(await screen.findByText('Choose a PDF to upload.')).toBeInTheDocument();
    expect(creations).toBe(0);
  });

  it('creates a PDF + keywords project with the chosen settings, uploads the PDF, then saves the keywords', async () => {
    const calls: Call[] = shared.calls;
    server.use(
      http.post(`${API}/v1/projects`, async ({ request }) => {
        const body = await request.json();
        calls.push({ path: 'project', key: request.headers.get('idempotency-key'), body });
        return HttpResponse.json(project({ source_type: 'pdf', title: 'Digital Electronics' }), {
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
    await fillSource();
    expect(screen.getByRole('list', { name: 'Keywords added' })).toHaveTextContent('Logic gates');
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));

    // Step 2: settings, defaulting to en-IN and 5:00. Any length to the second, not a preset.
    expect(
      await screen.findByRole('heading', { name: 'Configure generation settings' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /English \(India\)/ })).toBeChecked();
    const length = screen.getByLabelText('Target duration, minutes and seconds');
    expect(length).toHaveValue('5:00');
    await userEvent.clear(length);
    await userEvent.type(length, '7:30');
    await userEvent.click(screen.getByRole('radio', { name: /Vertical/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));

    // Step 3: review, then create.
    expect(await screen.findByRole('heading', { name: 'Review and create' })).toBeInTheDocument();
    expect(screen.getByText('Digital Electronics')).toBeInTheDocument();
    expect(screen.getByText('7 min 30 s')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Create project' }));

    await waitFor(() => expect(router.push).toHaveBeenCalledWith(`/projects/${PROJECT_ID}/topics`));
    expect(calls.map((call) => call.path)).toEqual(['project', 'upload', 'keywords']);
    expect(calls[0]?.body).toEqual({
      title: 'Digital Electronics',
      source_type: 'pdf',
      language: 'en-IN',
      orientation: 'vertical',
      target_duration_ms: 450_000,
    });
    expect(calls[2]?.body).toEqual({ keywords: ['Logic gates', 'Flip-flops'] });
    expect(calls.every((call) => call.key && call.key.length >= 8)).toBe(true);
    expect(new Set(calls.map((call) => call.key)).size).toBe(3);
  });

  it('creates a PowerPoint project: asks for a .pptx, names it from the file, uploads it, saves no keywords', async () => {
    const calls: Call[] = shared.calls;
    server.use(
      http.post(`${API}/v1/projects`, async ({ request }) => {
        calls.push({
          path: 'project',
          key: request.headers.get('idempotency-key'),
          body: await request.json(),
        });
        return HttpResponse.json(project({ source_type: 'pptx' }), { status: 201 });
      }),
    );
    renderWithClient(<CreateProjectWizard />);

    await userEvent.click(screen.getByRole('radio', { name: /^PowerPoint deck/ }));
    const dropzone = screen.getByLabelText(/drag and drop your powerpoint deck/i);
    expect(dropzone).toHaveAttribute('accept', expect.stringContaining('.pptx'));
    expect(screen.queryByLabelText('Add a keyword')).not.toBeInTheDocument();

    await userEvent.upload(dropzone, DECK, { applyAccept: false });
    expect(screen.getByLabelText(/project title/i)).toHaveValue('Safety induction');
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Next' }));
    expect(await screen.findByText('PowerPoint deck')).toBeInTheDocument();
    await userEvent.click(await screen.findByRole('button', { name: 'Create project' }));

    await waitFor(() => expect(router.push).toHaveBeenCalledWith(`/projects/${PROJECT_ID}/topics`));
    expect(calls.map((call) => call.path)).toEqual(['project', 'upload']);
    expect(calls[0]?.body).toMatchObject({ source_type: 'pptx', title: 'Safety induction' });
    expect(calls[1]?.body).toEqual({ file: 'safety_induction.pptx' });
  });

  it('says what is wrong with a file that is not a deck, including an older .ppt', async () => {
    renderWithClient(<CreateProjectWizard />);
    await userEvent.click(screen.getByRole('radio', { name: /^PowerPoint deck/ }));
    const dropzone = screen.getByLabelText(/drag and drop your powerpoint deck/i);

    await userEvent.type(screen.getByLabelText(/project title/i), 'Induction');
    await userEvent.upload(dropzone, new File([new Uint8Array(10)], 'old.ppt'), {
      applyAccept: false,
    });
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(await screen.findByText(/older \.ppt files are not supported/i)).toBeInTheDocument();

    await userEvent.upload(dropzone, PDF, { applyAccept: false });
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(await screen.findByText(/that is not a powerpoint deck/i)).toBeInTheDocument();
  });

  it('keeps the project and offers a retry when a later step fails, instead of starting over', async () => {
    let keywordAttempts = 0;
    let projectCreations = 0;
    server.use(
      http.post(`${API}/v1/projects`, () => {
        projectCreations += 1;
        return HttpResponse.json(project({ source_type: 'pdf' }), { status: 201 });
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
    await fillSource();
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
