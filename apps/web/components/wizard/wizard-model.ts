import type { Language, Orientation, ProjectSourceType } from '@edutoon/contracts';
import { durationProblem } from '@/lib/generation-settings';

/**
 * The create-project form: what is asked for, and the checks made before anything is sent.
 * The API checks everything again; these limits mirror its defaults so a mistake is caught
 * before an upload, with a message that says what to do.
 */

export const MAX_UPLOAD_MB = 100;
export const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024;
export const MAX_KEYWORDS = 30;
export const MAX_KEYWORD_CHARS = 120;
export const MAX_TITLE_CHARS = 200;

export type SourceMode = 'pdf' | 'keywords' | 'both';

/**
 * Whether a project can be built from keywords alone. It cannot yet: every claim in a video is
 * checked against the person's own material, and keywords alone give nothing to check against, so
 * the API refuses to create one. The choice stays visible (people ask for it) but cannot be picked.
 * PDF + keywords, where the keywords guide a PDF, is supported.
 */
export const KEYWORD_ONLY_SUPPORTED = false;

export const KEYWORD_ONLY_UNSUPPORTED_MESSAGE =
  'Keyword-only projects are not supported yet. Every claim in a video must be checked against your own source material, and keywords alone give nothing to check against. Upload a PDF - you can add keywords to guide it.';

export const isModeAvailable = (mode: SourceMode): boolean =>
  mode !== 'keywords' || KEYWORD_ONLY_SUPPORTED;

export interface WizardValues {
  title: string;
  mode: SourceMode;
  file: File | null;
  keywords: string[];
  orientation: Orientation;
  /** The target length in milliseconds - any whole number of seconds in range. */
  durationMs: number;
  language: Language;
}

export const usesPdf = (mode: SourceMode): boolean => mode === 'pdf' || mode === 'both';
export const usesKeywords = (mode: SourceMode): boolean => mode === 'keywords' || mode === 'both';

/** The API has two source types; "PDF + keywords" is a PDF project that also has keywords. */
export function projectSourceType(mode: SourceMode): ProjectSourceType {
  return mode === 'keywords' ? 'topic' : 'pdf';
}

/** A readable problem with a chosen file, or null if it is fine. */
export function fileProblem(file: File): string | null {
  const looksLikePdf = file.type === 'application/pdf' || /\.pdf$/i.test(file.name);
  if (!looksLikePdf) return 'That is not a PDF. Choose a file that ends in .pdf.';
  if (file.size === 0) return 'That file is empty. Choose a different PDF.';
  if (file.size > MAX_UPLOAD_BYTES)
    return `That file is over ${MAX_UPLOAD_MB} MB. Choose a smaller PDF.`;
  return null;
}

/** Split typed or pasted text into keywords: commas, semicolons and new lines separate them. */
export function splitKeywords(raw: string): string[] {
  return raw
    .split(/[,;\n]/)
    .map((part) => part.replace(/\s+/g, ' ').trim())
    .filter((part) => part.length > 0);
}

export interface KeywordMerge {
  keywords: string[];
  tooLong: number;
  duplicates: number;
  overLimit: number;
}

/** Add typed text to the keyword list: case-insensitive duplicates, over-long and surplus ones are refused. */
export function mergeKeywords(current: readonly string[], raw: string): KeywordMerge {
  const keywords = [...current];
  const seen = new Set(keywords.map((keyword) => keyword.toLowerCase()));
  let tooLong = 0;
  let duplicates = 0;
  let overLimit = 0;

  for (const candidate of splitKeywords(raw)) {
    if (candidate.length > MAX_KEYWORD_CHARS) {
      tooLong += 1;
    } else if (seen.has(candidate.toLowerCase())) {
      duplicates += 1;
    } else if (keywords.length >= MAX_KEYWORDS) {
      overLimit += 1;
    } else {
      keywords.push(candidate);
      seen.add(candidate.toLowerCase());
    }
  }
  return { keywords, tooLong, duplicates, overLimit };
}

/** "data_structures-notes.pdf" becomes "Data structures notes". */
export function suggestTitle(fileName: string): string {
  const stem = fileName
    .replace(/\.pdf$/i, '')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return stem.length === 0 ? '' : stem.charAt(0).toUpperCase() + stem.slice(1);
}

export interface SourceErrors {
  title?: string;
  mode?: string;
  file?: string;
  keywords?: string;
}

export function sourceErrors(values: WizardValues): SourceErrors {
  const errors: SourceErrors = {};
  const title = values.title.trim();
  if (title.length === 0) errors.title = 'Give your project a name.';
  else if (title.length > MAX_TITLE_CHARS)
    errors.title = `Keep the name under ${MAX_TITLE_CHARS} characters.`;

  if (!isModeAvailable(values.mode)) errors.mode = KEYWORD_ONLY_UNSUPPORTED_MESSAGE;

  if (usesPdf(values.mode)) {
    if (!values.file) errors.file = 'Choose a PDF to upload.';
    else {
      const problem = fileProblem(values.file);
      if (problem) errors.file = problem;
    }
  }
  if (usesKeywords(values.mode) && values.keywords.length === 0) {
    errors.keywords = 'Add at least one keyword.';
  }
  return errors;
}

export const hasErrors = (errors: object): boolean => Object.keys(errors).length > 0;

export function settingsErrors(values: WizardValues): { duration?: string } {
  const problem = durationProblem(values.durationMs);
  return problem === null ? {} : { duration: problem };
}
