/**
 * What the product offers, decided in one place.
 *
 * Since 2026-10-18 EduToon AI offers exactly three ways to make a video: a PDF, pasted text, and a
 * PDF with keywords that focus the lesson. A topic with no document, a PowerPoint deck and free-text
 * instructions are switched off here and on the server (`KEYWORD_ONLY_PROJECTS_ENABLED`,
 * `PPTX_PROJECTS_ENABLED`, `INSTRUCTIONS_ENABLED`, all off by default). Their code and tests remain;
 * to offer one again, add it here and turn its server switch on.
 */
export type OfferedMode = 'pdf' | 'text' | 'both';

export const OFFERED_MODES: readonly string[] = ['pdf', 'text', 'both'];

/** Whether the wizard offers the free-text instructions box (and the workspace the card to edit them). */
export const INSTRUCTIONS_OFFERED = false;
