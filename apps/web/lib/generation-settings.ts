import type { Language, Orientation } from '@edutoon/contracts';

/**
 * The choices offered for a project's generation settings. The values are the API's own
 * (`Orientation`, `Language`); the limits mirror `edutoon.domain.generation_settings` and are
 * enforced by the API too, so a disagreement surfaces as a clear error, never a bad save.
 */

export const MIN_DURATION_MINUTES = 1;
export const MAX_DURATION_MINUTES = 60;
export const DEFAULT_DURATION_MINUTES = 5;

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

export const minutesToMs = (minutes: number): number => minutes * 60_000;
export const msToMinutes = (ms: number): number => Math.round(ms / 60_000);

export function isValidDurationMinutes(value: number): boolean {
  return Number.isInteger(value) && value >= MIN_DURATION_MINUTES && value <= MAX_DURATION_MINUTES;
}
