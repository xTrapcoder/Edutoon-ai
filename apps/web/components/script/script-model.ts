import type { ScriptDetail, ScriptSegment, Topic, VerdictStatus } from '@edutoon/contracts';
import type { Tone } from '@/lib/workflow';

export const VERDICT: Record<VerdictStatus, { label: string; tone: Tone }> = {
  supported: { label: 'Supported', tone: 'success' },
  partially_supported: { label: 'Partly supported', tone: 'warning' },
  unsupported: { label: 'Not supported', tone: 'danger' },
  contradicted: { label: 'Contradicted', tone: 'danger' },
};

/** A section's name: the topic it covers, or "Section N" if it is not tied to one. */
export function segmentTitle(segment: ScriptSegment, topics: readonly Topic[]): string {
  const topic = segment.topic_id
    ? topics.find((candidate) => candidate.id === segment.topic_id)
    : undefined;
  return topic?.title ?? `Section ${segment.segment_index + 1}`;
}

/** Narration is stored as plain text; blank lines separate paragraphs. */
export function paragraphs(text: string): string[] {
  return text
    .split(/\n{2,}/)
    .map((paragraph) => paragraph.replace(/\s*\n\s*/g, ' ').trim())
    .filter((paragraph) => paragraph.length > 0);
}

export function wordCount(text: string): number {
  const words = text.trim().split(/\s+/).filter(Boolean);
  return words.length;
}

export function scriptWordCount(script: ScriptDetail): number {
  return script.segments.reduce((total, segment) => total + wordCount(segment.narration_text), 0);
}

export interface DurationComparison {
  /** Signed: positive means the script is longer than the target. */
  differenceMs: number;
  /** A gentle description, or null when it is close enough not to mention. */
  note: string | null;
}

/** How the script's estimated length compares with the target the person chose. */
export function compareDuration(
  estimatedMs: number | null,
  targetMs: number,
): DurationComparison | null {
  if (estimatedMs === null || estimatedMs <= 0) return null;
  const differenceMs = estimatedMs - targetMs;
  const ratio = Math.abs(differenceMs) / targetMs;
  if (ratio <= 0.2) return { differenceMs, note: null };
  return {
    differenceMs,
    note: differenceMs < 0 ? 'Shorter than your target' : 'Longer than your target',
  };
}
