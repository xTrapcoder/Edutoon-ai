import { describe, expect, it } from 'vitest';
import {
  fileProblem,
  MAX_KEYWORDS,
  mergeKeywords,
  projectSourceType,
  sourceErrors,
  suggestTitle,
  type WizardValues,
} from './wizard-model';

const pdf = (name = 'notes.pdf', size = 1000, type = 'application/pdf') =>
  new File([new Uint8Array(size)], name, { type });

const values = (over: Partial<WizardValues> = {}): WizardValues => ({
  title: 'Networks',
  mode: 'pdf',
  file: pdf(),
  keywords: [],
  orientation: 'horizontal',
  durationMinutes: 5,
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
    expect(sourceErrors(values({ mode: 'keywords', file: null, keywords: [] }))).toEqual({
      keywords: 'Add at least one keyword.',
    });
    expect(sourceErrors(values({ mode: 'keywords', file: null, keywords: ['a'] }))).toEqual({});
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
