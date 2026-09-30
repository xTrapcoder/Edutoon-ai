/**
 * Display formatting. British English, and 24-hour time everywhere.
 *
 * Every duration in the API is an integer number of milliseconds; these helpers are the only
 * place that turns one into words.
 */

const LOCALE = 'en-GB';

const MS_PER_MINUTE = 60_000;

/** "1 minute", "5 minutes", "1 h 30 min" - for target and estimated durations. */
export function formatDuration(ms: number): string {
  const totalMinutes = Math.round(ms / MS_PER_MINUTE);
  if (totalMinutes < 1) {
    const seconds = Math.max(1, Math.round(ms / 1000));
    return `${seconds} sec`;
  }
  if (totalMinutes < 60) {
    return totalMinutes === 1 ? '1 minute' : `${totalMinutes} minutes`;
  }
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return minutes === 0 ? `${hours} h` : `${hours} h ${minutes} min`;
}

/**
 * A length the person chose, exactly: "5 minutes" for a whole number of minutes, otherwise
 * "3 min 45 s". Unlike {@link formatDuration} it never rounds, because a target can be any
 * whole number of seconds.
 */
export function formatTargetDuration(ms: number): string {
  const totalSeconds = Math.round(ms / 1000);
  if (totalSeconds % 60 === 0) return formatDuration(totalSeconds * 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes === 0 ? `${seconds} s` : `${minutes} min ${String(seconds).padStart(2, '0')} s`;
}

/** "2 min 05 s" - for how long a running job has been going. */
export function formatElapsed(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes === 0) return `${seconds} s`;
  return `${minutes} min ${String(seconds).padStart(2, '0')} s`;
}

const dateTimeFormat = new Intl.DateTimeFormat(LOCALE, {
  day: 'numeric',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

const dateFormat = new Intl.DateTimeFormat(LOCALE, {
  day: 'numeric',
  month: 'short',
  year: 'numeric',
});

/** "26 Sept 2026, 21:31" */
export function formatDateTime(iso: string): string {
  return dateTimeFormat.format(new Date(iso));
}

export function formatDate(iso: string): string {
  return dateFormat.format(new Date(iso));
}

const relativeFormat = new Intl.RelativeTimeFormat(LOCALE, { numeric: 'auto' });

const RELATIVE_STEPS: readonly { unit: Intl.RelativeTimeFormatUnit; ms: number }[] = [
  { unit: 'day', ms: 86_400_000 },
  { unit: 'hour', ms: 3_600_000 },
  { unit: 'minute', ms: 60_000 },
];

/** "just now", "5 minutes ago", "yesterday", then a plain date once it is over a week old. */
export function formatRelative(iso: string, now: number = Date.now()): string {
  const elapsed = now - new Date(iso).getTime();
  if (elapsed < 45_000) return 'just now';
  if (elapsed > 7 * 86_400_000) return formatDate(iso);
  for (const step of RELATIVE_STEPS) {
    if (elapsed >= step.ms) {
      return relativeFormat.format(-Math.floor(elapsed / step.ms), step.unit);
    }
  }
  return relativeFormat.format(-1, 'minute');
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** "Page 4" or "Pages 4-6" (an en dash, as in print). */
export function formatPageRange(from: number | null, to: number | null): string | null {
  if (from === null && to === null) return null;
  const start = from ?? to;
  const end = to ?? from;
  if (start === null || end === null) return null;
  return start === end ? `Page ${start}` : `Pages ${start}–${end}`;
}

export function pluralise(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

/** A greeting for the local time of day, on the 24-hour clock. */
export function greetingFor(date: Date): string {
  const hour = date.getHours();
  if (hour < 5) return 'Good evening';
  if (hour < 12) return 'Good morning';
  if (hour < 18) return 'Good afternoon';
  return 'Good evening';
}
