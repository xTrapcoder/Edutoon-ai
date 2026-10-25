import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { project, script } from '@/test/fixtures';
import { renderWithClient } from '@/test/render';
import { API, server } from '@/test/server';
import { AssessmentEditor, draftProblem } from './assessment-editor';
import { AddModuleCard } from './course-detail';
import { ModuleView } from './module-view';
import { MyCoursesView } from './my-courses';

const AT = '2026-10-17T10:00:00.000Z';
const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;

vi.mock('@/lib/api/training', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/training')>()),
  // A real video is a blob fetched with the person's token; jsdom has no media, so a stand-in URL.
  fetchModuleVideoUrl: vi.fn(async () => 'blob:test-video'),
}));

const module1 = (over: Record<string, unknown> = {}) => ({
  id: id(101),
  position: 1,
  title: 'Fire safety',
  description: null,
  duration_ms: 480_000,
  width: 1280,
  height: 720,
  locked: false,
  lock: null,
  ...over,
});
const module2 = (over: Record<string, unknown> = {}) => ({
  id: id(102),
  position: 2,
  title: 'First aid',
  description: null,
  duration_ms: 300_000,
  width: 1280,
  height: 720,
  locked: true,
  lock: {
    reason: 'earlier_module_not_passed',
    blocking_module_id: id(101),
    blocking_module_position: 1,
    blocking_module_title: 'Fire safety',
  },
  ...over,
});
const course = (modules: unknown[]) => ({
  id: id(1),
  title: 'Workplace safety',
  description: 'Start here.',
  modules,
});

const attempt = (over: Record<string, unknown> = {}) => ({
  id: id(500),
  module_id: id(101),
  attempt_number: 1,
  correct_count: 1,
  question_count: 2,
  threshold_percent: 70,
  passed: false,
  submitted_at: AT,
  ...over,
});

const assessment = {
  module_id: id(101),
  threshold_percent: 70,
  question_count: 2,
  questions: [
    {
      id: id(201),
      position: 1,
      prompt: 'What do you do first when the alarm sounds?',
      options: [
        { id: id(211), position: 1, label: 'Leave by the nearest exit' },
        { id: id(212), position: 2, label: 'Finish your email' },
      ],
    },
    {
      id: id(202),
      position: 2,
      prompt: 'Where is the assembly point?',
      options: [
        { id: id(221), position: 1, label: 'In the car park' },
        { id: id(222), position: 2, label: 'At your desk' },
      ],
    },
  ],
};

function serveLearner(opts: {
  modules?: unknown[];
  attempts?: ReturnType<typeof attempt>[];
  hasAssessment?: boolean;
}) {
  server.use(
    http.get(`${API}/v1/training/courses`, () =>
      HttpResponse.json({ items: [course(opts.modules ?? [module1(), module2()])] }),
    ),
    http.get(`${API}/v1/training/modules/:moduleId/assessment`, () =>
      opts.hasAssessment === false
        ? HttpResponse.json(
            { error: { code: 'not_found', message: 'Not found.' } },
            { status: 404 },
          )
        : HttpResponse.json(assessment),
    ),
    http.get(`${API}/v1/training/modules/:moduleId/assessment/attempts`, () =>
      HttpResponse.json({ items: opts.attempts ?? [] }),
    ),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('My learning', () => {
  it('opens the first module, shows later ones locked and says which module to pass', async () => {
    serveLearner({});
    renderWithClient(<MyCoursesView />);

    const modules = await screen.findByRole('list', { name: 'Modules in Workplace safety' });
    expect(within(modules).getByRole('link', { name: /Fire safety/ })).toHaveAttribute(
      'href',
      `/learn/${id(101)}`,
    );
    const locked = within(modules).getByRole('listitem', { name: 'First aid, locked' });
    expect(within(locked).queryByRole('link')).not.toBeInTheDocument();
    expect(locked).toHaveTextContent(/Pass .Fire safety. \(module 1\) to unlock this one/);
  });

  it('shows how far the person has got: passed, not passed yet, or ready to start', async () => {
    serveLearner({ attempts: [attempt({ correct_count: 2, passed: true })] });
    renderWithClient(<MyCoursesView />);
    expect(await screen.findByText(/Passed · 2\/2/)).toBeInTheDocument();
  });

  it('says so when nothing has been assigned', async () => {
    server.use(http.get(`${API}/v1/training/courses`, () => HttpResponse.json({ items: [] })));
    renderWithClient(<MyCoursesView />);
    expect(await screen.findByText('Nothing assigned to you yet')).toBeInTheDocument();
  });
});

describe('A module', () => {
  it('plays the video and asks the questions without any answer key', async () => {
    serveLearner({});
    renderWithClient(<ModuleView moduleId={id(101)} />);

    expect(await screen.findByLabelText('Video: Fire safety')).toHaveAttribute(
      'src',
      'blob:test-video',
    );
    expect(await screen.findByText(/pass mark 70%/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Submit answers' })).toBeDisabled();
    expect(document.body.textContent).not.toMatch(/is_correct|correct option/i);
  });

  it('submits every answer once, locks them, and offers another try after a fail', async () => {
    serveLearner({});
    let submitted: unknown = null;
    server.use(
      http.post(`${API}/v1/training/modules/:moduleId/assessment/attempts`, async ({ request }) => {
        submitted = { body: await request.json(), key: request.headers.get('idempotency-key') };
        return HttpResponse.json(attempt({ correct_count: 1, question_count: 2 }), { status: 201 });
      }),
    );
    renderWithClient(<ModuleView moduleId={id(101)} />);

    await userEvent.click(await screen.findByLabelText('Leave by the nearest exit'));
    await userEvent.click(screen.getByLabelText('At your desk'));
    await userEvent.click(screen.getByRole('button', { name: 'Submit answers' }));

    expect(await screen.findByText('Not passed this time')).toBeInTheDocument();
    expect(screen.getByText(/1 of 2 correct \(50%\); the pass mark is 70%/)).toBeInTheDocument();
    expect(submitted).toMatchObject({
      body: {
        answers: [
          { question_id: id(201), option_id: id(211) },
          { question_id: id(202), option_id: id(222) },
        ],
      },
    });
    expect((submitted as { key: string }).key.length).toBeGreaterThan(8);
    // Submitted answers cannot be changed...
    for (const radio of screen.getAllByRole('radio')) expect(radio).toBeDisabled();
    // ...but a failed attempt can be tried again, from clean.
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    for (const radio of screen.getAllByRole('radio')) {
      expect(radio).toBeEnabled();
      expect(radio).not.toBeChecked();
    }
  });

  it('on a pass says so and links to the next module', async () => {
    serveLearner({});
    server.use(
      http.post(`${API}/v1/training/modules/:moduleId/assessment/attempts`, () =>
        HttpResponse.json(attempt({ correct_count: 2, passed: true }), { status: 201 }),
      ),
    );
    renderWithClient(<ModuleView moduleId={id(101)} />);

    await userEvent.click(await screen.findByLabelText('Leave by the nearest exit'));
    await userEvent.click(screen.getByLabelText('In the car park'));
    await userEvent.click(screen.getByRole('button', { name: 'Submit answers' }));

    expect(await screen.findByText('You passed')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Next module: First aid/ })).toHaveAttribute(
      'href',
      `/learn/${id(102)}`,
    );
  });

  it('remembers a pass after a reload: no questions, the best result, the next module', async () => {
    serveLearner({ attempts: [attempt({ correct_count: 2, passed: true })] });
    renderWithClient(<ModuleView moduleId={id(101)} />);

    expect(await screen.findByText('You have passed this module')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Submit answers' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Next module: First aid/ })).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Your attempts' })).toHaveTextContent('Attempt 1');
  });

  it('keeps a locked module shut and names what to pass first', async () => {
    serveLearner({});
    renderWithClient(<ModuleView moduleId={id(102)} />);

    expect(await screen.findByText('This module is locked')).toBeInTheDocument();
    expect(screen.queryByLabelText(/Video:/)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Go to module 1' })).toHaveAttribute(
      'href',
      `/learn/${id(101)}`,
    );
  });

  it('says a module that is not theirs is not available, however it came to be asked for', async () => {
    serveLearner({});
    renderWithClient(<ModuleView moduleId={id(999)} />);
    expect(await screen.findByText('This module is not available to you')).toBeInTheDocument();
  });

  it('copes with a module that has no assessment', async () => {
    serveLearner({ hasAssessment: false });
    renderWithClient(<ModuleView moduleId={id(101)} />);
    expect(await screen.findByText(/This module has no assessment/)).toBeInTheDocument();
  });
});

describe('Naming a module when publishing a video', () => {
  const readyProject = project({ id: id(700), title: 'Industrial Revolution', status: 'ready' });

  const setUp = () => {
    let published: unknown = null;
    server.use(
      http.get(`${API}/v1/projects`, () =>
        HttpResponse.json({ items: [readyProject], next_cursor: null }),
      ),
      http.get(`${API}/v1/projects/:projectId/scripts`, () =>
        HttpResponse.json({
          items: [script({ id: id(701), project_id: readyProject.id, status: 'approved' })],
          next_cursor: null,
        }),
      ),
      http.post(`${API}/v1/admin/training/courses/:courseId/modules`, async ({ request }) => {
        published = await request.json();
        return HttpResponse.json({ id: id(702) }, { status: 201 });
      }),
    );
    renderWithClient(<AddModuleCard courseId={id(1)} />);
    return () => published;
  };

  // The options arrive after the project list has loaded.
  const choose = async () => {
    await screen.findByRole('option', { name: 'Industrial Revolution' });
    await userEvent.selectOptions(screen.getByLabelText('Project'), 'Industrial Revolution');
  };

  it('starts from the project name, and a person who edits it sends exactly what is in the field', async () => {
    // Regression: a QA module was saved as "Industrial ReThe Industrial Revolutionvolution" - the
    // person clicked into the prefilled field and typed mid-text. The field must behave like any
    // text field (the prefill is ordinary text, nothing is appended), and what is sent is what shows.
    const sent = setUp();
    await choose();
    const field = screen.getByLabelText('Module title');
    expect(field).toHaveValue('Industrial Revolution');

    await userEvent.clear(field);
    await userEvent.type(field, 'The Industrial Revolution');
    expect(field).toHaveValue('The Industrial Revolution');

    await userEvent.click(await screen.findByRole('button', { name: 'Publish as a module' }));
    await waitFor(() => expect(sent()).not.toBeNull());
    expect(sent()).toMatchObject({ title: 'The Industrial Revolution' });
  });

  it('sends the prefilled name unchanged when nobody edits it', async () => {
    const sent = setUp();
    await choose();
    await userEvent.click(await screen.findByRole('button', { name: 'Publish as a module' }));
    await waitFor(() => expect(sent()).not.toBeNull());
    expect(sent()).toMatchObject({ title: 'Industrial Revolution' });
  });

  it('leaves the title out when it is cleared, so the server names the module', async () => {
    const sent = setUp();
    await choose();
    await userEvent.clear(screen.getByLabelText('Module title'));
    await userEvent.click(await screen.findByRole('button', { name: 'Publish as a module' }));
    await waitFor(() => expect(sent()).not.toBeNull());
    expect((sent() as Record<string, unknown>).title).toBeUndefined();
  });
});

describe('Writing an assessment', () => {
  it('needs wording, two options and exactly one correct one for every question', () => {
    const ok = {
      prompt: 'Why?',
      options: [
        { label: 'A', correct: true },
        { label: 'B', correct: false },
      ],
    };
    expect(draftProblem([])).toBe('Add at least one question.');
    expect(draftProblem([ok])).toBeNull();
    expect(draftProblem([{ ...ok, prompt: ' ' }])).toBe('Question 1 needs wording.');
    expect(draftProblem([{ ...ok, options: [ok.options[0]!] }])).toBe(
      'Question 1 needs at least two options.',
    );
    expect(draftProblem([{ ...ok, options: [{ label: '', correct: true }, ok.options[1]!] }])).toBe(
      'Every option of question 1 needs wording.',
    );
    expect(
      draftProblem([
        {
          ...ok,
          options: [
            { label: 'A', correct: true },
            { label: 'B', correct: true },
          ],
        },
      ]),
    ).toBe('Question 1 needs exactly one correct option.');
    expect(
      draftProblem([ok, { ...ok, options: ok.options.map((o) => ({ ...o, correct: false })) }]),
    ).toBe('Question 2 needs exactly one correct option.');
  });

  it('saves the whole assessment, then publishes what was saved', async () => {
    const calls: string[] = [];
    let saved: unknown = null;
    const view = (status: string) => ({
      id: id(300),
      module_id: id(101),
      status,
      published_at: null,
      threshold_percent: 70,
      question_count: 1,
      questions: [],
      updated_at: AT,
    });
    server.use(
      http.get(`${API}/v1/admin/training/modules/:moduleId/assessment`, () =>
        HttpResponse.json({ error: { code: 'not_found', message: 'Not found.' } }, { status: 404 }),
      ),
      http.put(`${API}/v1/admin/training/modules/:moduleId/assessment`, async ({ request }) => {
        calls.push('put');
        saved = await request.json();
        return HttpResponse.json(view('draft'));
      }),
      http.post(`${API}/v1/admin/training/modules/:moduleId/assessment/publish`, () => {
        calls.push('publish');
        return HttpResponse.json({ ...view('published'), active_enrolments_affected: 0 });
      }),
    );
    const trainingModule = {
      id: id(101),
      title: 'Fire safety',
    } as unknown as Parameters<typeof AssessmentEditor>[0]['module'];
    renderWithClient(<AssessmentEditor module={trainingModule} onClose={() => undefined} />);

    await userEvent.type(
      await screen.findByPlaceholderText('The question, in words'),
      'Where to go?',
    );
    await userEvent.type(screen.getByLabelText('Question 1, option 1'), 'The exit');
    await userEvent.type(screen.getByLabelText('Question 1, option 2'), 'The lift');
    await userEvent.click(screen.getByRole('button', { name: 'Save and publish' }));

    await waitFor(() => expect(calls).toEqual(['put', 'publish']));
    expect(saved).toEqual({
      threshold_percent: 70,
      questions: [
        {
          prompt: 'Where to go?',
          options: [
            { label: 'The exit', is_correct: true },
            { label: 'The lift', is_correct: false },
          ],
        },
      ],
    });
  });
});
