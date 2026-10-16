import { describe, expect, it } from 'vitest';
import {
  fileKindOf,
  fileProblem,
  isModeAvailable,
  KEYWORD_ONLY_SUPPORTED,
  KEYWORD_ONLY_UNSUPPORTED_MESSAGE,
  MAX_KEYWORDS,
  MAX_TEXT_CHARS,
  mergeKeywords,
  MIN_TEXT_CHARS,
  projectSourceType,
  settingsErrors,
  sourceErrors,
  suggestTitle,
  textProblem,
  usesDeck,
  usesFile,
  usesKeywords,
  usesText,
  type WizardValues,
} from './wizard-model';

const pdf = (name = 'notes.pdf', size = 1000, type = 'application/pdf') =>
  new File([new Uint8Array(size)], name, { type });

const values = (over: Partial<WizardValues> = {}): WizardValues => ({
  title: 'Networks',
  mode: 'pdf',
  file: pdf(),
  keywords: [],
  text: '',
  orientation: 'horizontal',
  durationMs: 300_000,
  language: 'en-IN',
  ...over,
});

describe('mergeKeywords', () => {
  it('splits pasted lists and keeps them in order', () => {
    expect(mergeKeywords([], 'Logic gates, Boolean algebra;\nFlip-flops').keywords).toEqual([
      'Logic gates',
      'Boolean algebra',
      'Flip-flops',
    ]);
  });

  it('refuses a duplicate whatever its case, and says how many', () => {
    const merged = mergeKeywords(['Logic gates'], 'logic GATES, Latches');
    expect(merged.keywords).toEqual(['Logic gates', 'Latches']);
    expect(merged.duplicates).toBe(1);
  });

  it('stops at the limit and reports the surplus', () => {
    const full = Array.from({ length: MAX_KEYWORDS }, (_, i) => `topic ${i}`);
    const merged = mergeKeywords(full, 'one more');
    expect(merged.keywords).toHaveLength(MAX_KEYWORDS);
    expect(merged.overLimit).toBe(1);
  });
});

describe('fileProblem', () => {
  it('accepts a PDF', () => {
    expect(fileProblem(pdf())).toBeNull();
  });

  it('refuses other files, empty files and huge files, in plain words', () => {
    expect(fileProblem(pdf('notes.docx', 10, 'application/msword'))).toMatch(/not a PDF/);
    expect(fileProblem(pdf('empty.pdf', 0))).toMatch(/empty/);
    const huge = { name: 'big.pdf', type: 'application/pdf', size: 101 * 1024 * 1024 } as File;
    expect(fileProblem(huge)).toMatch(/over 100 MB/);
  });
});

describe('sourceErrors', () => {
  it('asks for what the chosen source needs, and only that', () => {
    expect(sourceErrors(values({ title: ' ', file: null }))).toEqual({
      title: 'Give your project a name.',
      file: 'Choose a PDF to upload.',
    });
    expect(sourceErrors(values({ mode: 'both', file: null, keywords: [] }))).toEqual({
      file: 'Choose a PDF to upload.',
      keywords: 'Add at least one keyword.',
    });
    expect(sourceErrors(values({ mode: 'both', keywords: ['a'] }))).toEqual({});
  });
});

describe('keyword-only projects', () => {
  it('are not supported yet, and the only mode that is refused', () => {
    expect(KEYWORD_ONLY_SUPPORTED).toBe(false);
    expect(isModeAvailable('keywords')).toBe(false);
    expect(isModeAvailable('pdf')).toBe(true);
    expect(isModeAvailable('both')).toBe(true);
  });

  it('cannot be submitted even with keywords filled in', () => {
    const errors = sourceErrors(values({ mode: 'keywords', file: null, keywords: ['a'] }));
    expect(errors.mode).toBe(KEYWORD_ONLY_UNSUPPORTED_MESSAGE);
    expect(Object.keys(errors)).toEqual(['mode']);
  });

  it('say why, and what to do instead, in the same words the API uses', () => {
    expect(KEYWORD_ONLY_UNSUPPORTED_MESSAGE).toContain(
      'Keyword-only projects are not supported yet',
    );
    expect(KEYWORD_ONLY_UNSUPPORTED_MESSAGE).toContain('Upload a PDF');
  });
});

describe('settingsErrors', () => {
  it('accepts any whole number of seconds from 1:00 to 10:00', () => {
    for (const durationMs of [60_000, 61_000, 225_000, 331_000, 600_000]) {
      expect(settingsErrors(values({ durationMs }))).toEqual({});
    }
  });

  it('refuses a length that is too short, or longer than can be made yet, and says which', () => {
    expect(settingsErrors(values({ durationMs: 59_000 })).duration).toMatch(/shorter than 1:00/);
    expect(settingsErrors(values({ durationMs: 2_400_000 })).duration).toMatch(
      /longer than 30:00 are not supported yet/,
    );
  });
});

describe('PowerPoint decks', () => {
  const deck = (name = 'induction.pptx', size = 10, type = '') =>
    new File([new Uint8Array(size)], name, { type });

  it('accepts a .pptx by its name or its type, and refuses everything else with the reason', () => {
    expect(fileProblem(deck(), 'pptx')).toBeNull();
    expect(
      fileProblem(
        deck('x', 10, 'application/vnd.openxmlformats-officedocument.presentationml.presentation'),
        'pptx',
      ),
    ).toBeNull();
    expect(fileProblem(deck('notes.docx'), 'pptx')).toMatch(/not a PowerPoint deck/);
    expect(fileProblem(deck('old.ppt'), 'pptx')).toMatch(/Older \.ppt files are not supported/);
    expect(fileProblem(deck('empty.pptx', 0), 'pptx')).toMatch(/empty/);
    expect(fileProblem(deck('induction.pptx'))).toMatch(/not a PDF/); // a PDF is still the default
  });

  it('is a project source of its own, and a file project, but has no keywords', () => {
    expect(projectSourceType('pptx')).toBe('pptx');
    expect(usesFile('pptx')).toBe(true);
    expect(usesDeck('pptx')).toBe(true);
    expect(usesKeywords('pptx')).toBe(false);
    expect(fileKindOf('pptx')).toBe('pptx');
    expect(fileKindOf('both')).toBe('pdf');
  });

  it('names a project from the deck file, dropping .pptx', () => {
    expect(suggestTitle('data_structures-notes.pptx')).toBe('Data structures notes');
  });

  it('asks for a deck, not a PDF, when none is chosen', () => {
    const errors = sourceErrors({ ...values(), mode: 'pptx', file: null });
    expect(errors.file).toBe('Choose a PowerPoint deck to upload.');
  });
});

describe('pasted text', () => {
  const enough = 'Plants make food from light. '.repeat(20);

  it('is a project source of its own, needs no file and no keywords', () => {
    expect(projectSourceType('text')).toBe('text');
    expect(usesText('text')).toBe(true);
    expect(usesFile('text')).toBe(false);
    expect(usesKeywords('text')).toBe(false);
    expect(isModeAvailable('text')).toBe(true);
    expect(sourceErrors(values({ mode: 'text', file: null, text: enough }))).toEqual({});
  });

  it("asks for text, and says how much is needed, using the API's own limits", () => {
    expect([MIN_TEXT_CHARS, MAX_TEXT_CHARS]).toEqual([300, 200_000]);
    expect(textProblem('   ')).toMatch(/paste the material/i);
    expect(textProblem('x'.repeat(MIN_TEXT_CHARS - 1))).toMatch(/at least 300 characters.*299/);
    expect(textProblem('x'.repeat(MIN_TEXT_CHARS))).toBeNull();
    expect(textProblem('x'.repeat(MAX_TEXT_CHARS))).toBeNull();
    expect(textProblem('x'.repeat(MAX_TEXT_CHARS + 1))).toMatch(/limit is 200,000/);
  });

  it('counts the text without its surrounding blank space', () => {
    expect(textProblem(`\n\n  ${'x'.repeat(MIN_TEXT_CHARS)}  \n`)).toBeNull();
    expect(sourceErrors(values({ mode: 'text', file: null, text: 'short' })).text).toBeDefined();
  });
});

describe('projectSourceType', () => {
  it('sends a PDF-plus-keywords project to the API as a PDF project', () => {
    expect(projectSourceType('pdf')).toBe('pdf');
    expect(projectSourceType('both')).toBe('pdf');
    expect(projectSourceType('keywords')).toBe('topic');
  });
});

describe('suggestTitle', () => {
  it('turns a file name into a title', () => {
    expect(suggestTitle('data_structures-notes.pdf')).toBe('Data structures notes');
  });
});
