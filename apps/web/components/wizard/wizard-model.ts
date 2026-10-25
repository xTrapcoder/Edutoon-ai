import type { Language, Orientation, ProjectSourceType } from '@edutoon/contracts';
import { durationProblem } from '@/lib/generation-settings';
import { INSTRUCTIONS_OFFERED, OFFERED_MODES } from '@/lib/scope';

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
/** Mirrors `domain.pasted_text` in the API: the least and most pasted text it accepts. */
export const MIN_TEXT_CHARS = 300;
export const MAX_TEXT_CHARS = 200_000;
/** Mirrors `domain.instructions` in the API: the most instruction text it accepts. */
export const MAX_INSTRUCTIONS_CHARS = 2_000;

export type SourceMode = 'pdf' | 'pptx' | 'text' | 'both' | 'instructions' | 'scan';

/** What a person is told when a mode is not offered. */
export const MODE_UNAVAILABLE_MESSAGE =
  'That way of making a video is not offered. Upload a PDF or paste your own text instead.';

/** Whether the wizard offers a mode at all (`lib/scope.ts`): the five offered modes only. */
export const isModeAvailable = (mode: SourceMode): boolean => OFFERED_MODES.includes(mode);

export interface WizardValues {
  title: string;
  mode: SourceMode;
  file: File | null;
  keywords: string[];
  /** The pasted material, for the 'text' mode. */
  text: string;
  /** What the person asks of the lesson in their own words; blank means none. */
  instructions: string;
  orientation: Orientation;
  /** The target length in milliseconds - any whole number of seconds in range. */
  durationMs: number;
  language: Language;
}

export const usesPdf = (mode: SourceMode): boolean =>
  mode === 'pdf' || mode === 'both' || mode === 'instructions' || mode === 'scan';
export const usesDeck = (mode: SourceMode): boolean => mode === 'pptx';
/** Whether the project is made from an uploaded file (a PDF or a PowerPoint deck). */
export const usesFile = (mode: SourceMode): boolean => usesPdf(mode) || usesDeck(mode);
export const usesKeywords = (mode: SourceMode): boolean => mode === 'both';
export const usesText = (mode: SourceMode): boolean => mode === 'text';
/** Whether the mode asks for instructions: "PDF + instructions" does, and needs them. */
export const usesInstructions = (mode: SourceMode): boolean =>
  INSTRUCTIONS_OFFERED && mode === 'instructions';

/** Whether the mode is the scanned-PDF one, whose pages are read by local OCR. */
export const usesOcr = (mode: SourceMode): boolean => mode === 'scan';

/**
 * The API has three source types the product uses; "PDF + keywords", "PDF + instructions" and
 * "scanned PDF" are PDF projects (with keywords, with instructions, or read by OCR).
 */
export function projectSourceType(mode: SourceMode): ProjectSourceType {
  if (mode === 'pptx') return 'pptx';
  if (mode === 'text') return 'text';
  return 'pdf';
}

export type FileKind = 'pdf' | 'pptx';

export const PPTX_MIME =
  'application/vnd.openxmlformats-officedocument.presentationml.presentation';

/** The kind of file a mode asks for. */
export const fileKindOf = (mode: SourceMode): FileKind => (usesDeck(mode) ? 'pptx' : 'pdf');

/** A readable problem with a chosen file, or null if it is fine. */
export function fileProblem(file: File, kind: FileKind = 'pdf'): string | null {
  const word = kind === 'pptx' ? 'PowerPoint deck' : 'PDF';
  if (kind === 'pptx') {
    if (/\.ppt$/i.test(file.name) || file.type === 'application/vnd.ms-powerpoint')
      return 'Older .ppt files are not supported. Open it in PowerPoint and save it as .pptx.';
    if (file.type !== PPTX_MIME && !/\.pptx$/i.test(file.name))
      return 'That is not a PowerPoint deck. Choose a file that ends in .pptx.';
  } else {
    const looksLikePdf = file.type === 'application/pdf' || /\.pdf$/i.test(file.name);
    if (!looksLikePdf) return 'That is not a PDF. Choose a file that ends in .pdf.';
  }
  if (file.size === 0) return `That file is empty. Choose a different ${word}.`;
  if (file.size > MAX_UPLOAD_BYTES)
    return `That file is over ${MAX_UPLOAD_MB} MB. Choose a smaller ${word}.`;
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
    .replace(/\.(pdf|pptx)$/i, '')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return stem.length === 0 ? '' : stem.charAt(0).toUpperCase() + stem.slice(1);
}

/** A readable problem with pasted text, or null if it is fine. The API cleans the text first. */
export function textProblem(text: string): string | null {
  const length = text.trim().length;
  if (length === 0) return 'Paste the material you want explained.';
  if (length < MIN_TEXT_CHARS)
    return `Paste at least ${MIN_TEXT_CHARS} characters so there is enough to check a video against - you have ${length}.`;
  if (length > MAX_TEXT_CHARS)
    return `That is ${length.toLocaleString('en-GB')} characters; the limit is ${MAX_TEXT_CHARS.toLocaleString('en-GB')}. Paste the parts you need.`;
  return null;
}

export interface SourceErrors {
  title?: string;
  mode?: string;
  file?: string;
  text?: string;
  keywords?: string;
  instructions?: string;
}

/** A readable problem with instruction text, or null if it is fine (blank is checked separately). */
export function instructionsProblem(text: string): string | null {
  const length = text.replace(/\s+/g, ' ').trim().length;
  return length > MAX_INSTRUCTIONS_CHARS
    ? `Keep the instructions under ${MAX_INSTRUCTIONS_CHARS.toLocaleString('en-GB')} characters - you have ${length.toLocaleString('en-GB')}.`
    : null;
}

export function sourceErrors(values: WizardValues): SourceErrors {
  const errors: SourceErrors = {};
  const title = values.title.trim();
  if (title.length === 0) errors.title = 'Give your project a name.';
  else if (title.length > MAX_TITLE_CHARS)
    errors.title = `Keep the name under ${MAX_TITLE_CHARS} characters.`;

  if (!isModeAvailable(values.mode)) errors.mode = MODE_UNAVAILABLE_MESSAGE;

  if (usesFile(values.mode)) {
    const kind = fileKindOf(values.mode);
    if (!values.file)
      errors.file =
        kind === 'pptx' ? 'Choose a PowerPoint deck to upload.' : 'Choose a PDF to upload.';
    else {
      const problem = fileProblem(values.file, kind);
      if (problem) errors.file = problem;
    }
  }
  if (usesText(values.mode)) {
    const problem = textProblem(values.text);
    if (problem) errors.text = problem;
  }
  if (usesKeywords(values.mode) && values.keywords.length === 0) {
    errors.keywords = 'Add at least one keyword.';
  }
  if (usesInstructions(values.mode)) {
    const problem = instructionsProblem(values.instructions);
    if (problem) errors.instructions = problem;
    else if (values.instructions.trim() === '')
      errors.instructions = 'Say how you want the lesson, in your own words.';
  }
  return errors;
}

export const hasErrors = (errors: object): boolean => Object.keys(errors).length > 0;

export function settingsErrors(values: WizardValues): { duration?: string } {
  const problem = durationProblem(values.durationMs);
  return problem === null ? {} : { duration: problem };
}
