import type { Project } from '@edutoon/contracts';

/**
 * What a project's source is made of, in the words the screens use.
 *
 * A project is built from a PDF, from a PowerPoint deck, or (not supported yet) from keywords
 * alone. A PDF has pages and a deck has slides; the pipeline treats both the same way - one chunk
 * of evidence each - so only the wording and the file type differ. Everything that used to ask
 * "is it a PDF?" asks :func:`isFileProject` instead.
 */

type HasSource = Pick<Project, 'source_type'>;

export const isDeckProject = (project: HasSource): boolean => project.source_type === 'pptx';

/** A project that is built from an uploaded file (a PDF or a deck), not from keywords alone. */
export const isFileProject = (project: HasSource): boolean =>
  project.source_type === 'pdf' || project.source_type === 'pptx';

export interface SourceWords {
  /** "PDF" or "PowerPoint deck" - what the person uploads. */
  file: string;
  /** "PDF" or "presentation" - what it is called mid-sentence. */
  noun: string;
  /** "page" or "slide". */
  unit: string;
  /** "Page" or "Slide", for a label. */
  Unit: 'Page' | 'Slide';
}

const PDF_WORDS: SourceWords = { file: 'PDF', noun: 'PDF', unit: 'page', Unit: 'Page' };
const DECK_WORDS: SourceWords = {
  file: 'PowerPoint deck',
  noun: 'presentation',
  unit: 'slide',
  Unit: 'Slide',
};

export const sourceWords = (project: HasSource): SourceWords =>
  isDeckProject(project) ? DECK_WORDS : PDF_WORDS;
