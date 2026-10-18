/**
 * What the product offers, decided in one place.
 *
 * EduToon AI offers exactly five ways to make a video: a PDF, pasted text, a PDF with keywords that
 * focus the lesson, a PDF with natural-language instructions, and a scanned PDF (read by local OCR).
 * A topic with no document was removed on 2026-10-18 and is not in the code any more; a PowerPoint
 * deck project is dormant (`PPTX_PROJECTS_ENABLED`, off by default on the server) and not offered
 * here. To offer a dormant mode again, add it here and turn its server switch on.
 */
export type OfferedMode = 'pdf' | 'text' | 'both' | 'instructions' | 'scan';

export const OFFERED_MODES: readonly string[] = ['pdf', 'text', 'both', 'instructions', 'scan'];

/** Whether the workspace shows the card to edit a project's instructions (`INSTRUCTIONS_ENABLED`). */
export const INSTRUCTIONS_OFFERED = true;
