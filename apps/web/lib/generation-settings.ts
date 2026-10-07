import type { Language, Orientation } from '@edutoon/contracts';

/**
 * The choices offered for a project's generation settings. The values are the API's own
 * (`Orientation`, `Language`); the limits mirror `edutoon.domain.generation_settings` and are
 * enforced by the API too, so a disagreement surfaces as a clear error, never a bad save.
 *
 * **Duration.** A target is any number of whole seconds from one minute up to the longest video
 * that can be rendered today - never a list of preset lengths. The API stores milliseconds and can
 * hold anything from one to sixty minutes; what is *offered* is capped lower, because a longer
 * video cannot be made end to end yet (`MAX_RENDER_DURATION_MS` in the API, 30 minutes, is the
 * product's maximum). When the long-video work is done this constant is
 * the one number to raise.
 */

export const MS_PER_SECOND = 1000;
export const MS_PER_MINUTE = 60_000;

export const MIN_DURATION_MS = 1 * MS_PER_MINUTE;
/** The longest video that can be produced end to end today (mirrors the API's render limit). */
export const MAX_SUPPORTED_DURATION_MS = 30 * MS_PER_MINUTE;
export const DEFAULT_DURATION_MS = 5 * MS_PER_MINUTE;

export const DEFAULT_LANGUAGE: Language = 'en-IN';
export const DEFAULT_ORIENTATION: Orientation = 'horizontal';

export interface OrientationOption {
  value: Orientation;
  label: string;
  ratio: string;
  hint: string;
}

export const ORIENTATION_OPTIONS: readonly OrientationOption[] = [
  {
    value: 'horizontal',
    label: 'Horizontal',
    ratio: '16:9',
    hint: 'Best for YouTube and presentations',
  },
  {
    value: 'vertical',
    label: 'Vertical',
    ratio: '9:16',
    hint: 'Best for mobile and short-form content',
  },
];

export interface LanguageOption {
  value: Language;
  label: string;
  hint: string;
}

export const LANGUAGE_OPTIONS: readonly LanguageOption[] = [
  { value: 'en-IN', label: 'English (India)', hint: 'Recommended for Indian students' },
  { value: 'en-GB', label: 'English (UK)', hint: 'British English' },
];

export const orientationLabel = (value: Orientation): string =>
  ORIENTATION_OPTIONS.find((option) => option.value === value)?.label ?? value;

export const languageLabel = (value: Language): string =>
  LANGUAGE_OPTIONS.find((option) => option.value === value)?.label ?? value;

/** `m:ss` (or `h:mm:ss` from an hour): 225_000 is "3:45". Rounded down to whole seconds. */
export function formatClock(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / MS_PER_SECOND));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const pad = (value: number) => String(value).padStart(2, '0');
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
}

/**
 * A length as a person types it, in milliseconds - or `null` if it is not one. `5` is five
 * minutes; `5:30` and `05:30` are five and a half; seconds must be two digits below 60. Anything
 * else ("5.5", "90s", "five") is `null`: the box says what it accepts instead of guessing.
 */
export function parseClock(text: string): number | null {
  const match = /^(\d{1,3})(?::([0-5]\d))?$/.exec(text.trim());
  if (!match) return null;
  const minutes = Number(match[1]);
  const seconds = match[2] === undefined ? 0 : Number(match[2]);
  return minutes * MS_PER_MINUTE + seconds * MS_PER_SECOND;
}

/** Whether `ms` can be chosen: whole seconds, from one minute to the longest supported video. */
export function isValidDurationMs(ms: number): boolean {
  return (
    Number.isInteger(ms) &&
    ms % MS_PER_SECOND === 0 &&
    ms >= MIN_DURATION_MS &&
    ms <= MAX_SUPPORTED_DURATION_MS
  );
}

/** Why `ms` cannot be chosen, in words for the person - or `null` if it can. */
export function durationProblem(ms: number | null): string | null {
  const range = `${formatClock(MIN_DURATION_MS)} to ${formatClock(MAX_SUPPORTED_DURATION_MS)}`;
  if (ms === null) return `Enter a length from ${range}, such as 5 or 7:30.`;
  if (ms > MAX_SUPPORTED_DURATION_MS) {
    return `Videos longer than ${formatClock(MAX_SUPPORTED_DURATION_MS)} are not supported yet. Enter a length from ${range}.`;
  }
  if (ms < MIN_DURATION_MS)
    return `Videos shorter than ${formatClock(MIN_DURATION_MS)} are not supported. Enter a length from ${range}.`;
  return null;
}
