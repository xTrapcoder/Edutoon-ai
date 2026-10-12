import { describe, expect, it } from 'vitest';
import { formatTargetDuration } from './format';
import {
  DEFAULT_DURATION_MS,
  durationProblem,
  formatClock,
  isValidDurationMs,
  MAX_SUPPORTED_DURATION_MS,
  MIN_DURATION_MS,
  parseClock,
} from './generation-settings';

describe('the supported range', () => {
  it('is one minute up to ten, defaulting to five - the longest video rendering was verified to', () => {
    expect(MIN_DURATION_MS).toBe(60_000);
    expect(MAX_SUPPORTED_DURATION_MS).toBe(1_800_000);
    expect(DEFAULT_DURATION_MS).toBe(300_000);
  });
});

describe('formatClock', () => {
  it.each([
    [0, '0:00'],
    [59_999, '0:59'],
    [60_000, '1:00'],
    [225_000, '3:45'],
    [600_000, '10:00'],
    [1_800_000, '30:00'],
    [3_725_000, '1:02:05'],
  ])('writes %i ms as %s', (ms, text) => {
    expect(formatClock(ms)).toBe(text);
  });
});

describe('parseClock', () => {
  it.each([
    ['5', 300_000],
    ['05', 300_000],
    ['5:00', 300_000],
    ['5:30', 330_000],
    ['05:30', 330_000],
    ['10:00', 600_000],
    ['30:00', 1_800_000],
    ['0:59', 59_000],
    [' 7:15 ', 435_000],
    ['12', 720_000],
  ])('reads %j as %i ms', (text, ms) => {
    expect(parseClock(text)).toBe(ms);
  });

  it.each(['', ' ', 'five', '5.5', '5:5', '5:60', '5:99', '-1', '5:30:00', '90s', '5 min', '1000'])(
    'refuses %j rather than guessing',
    (text) => {
      expect(parseClock(text)).toBeNull();
    },
  );
});

describe('isValidDurationMs', () => {
  it('accepts any whole number of seconds from one minute to thirty, not just round minutes', () => {
    for (const ms of [60_000, 61_000, 225_000, 331_000, 599_000, 600_000, 1_799_000, 1_800_000]) {
      expect(isValidDurationMs(ms)).toBe(true);
    }
  });

  it('refuses shorter, longer, fractional and non-integer lengths', () => {
    for (const ms of [0, 59_000, 59_999, 1_800_001, 1_801_000, 3_600_000, 90_500, 1.5]) {
      expect(isValidDurationMs(ms)).toBe(false);
    }
  });

  it('does not turn into fixed buckets: every second between the limits is accepted', () => {
    let accepted = 0;
    for (let ms = MIN_DURATION_MS; ms <= MAX_SUPPORTED_DURATION_MS; ms += 1000) {
      if (isValidDurationMs(ms)) accepted += 1;
    }
    expect(accepted).toBe(1741);
  });
});

describe('durationProblem', () => {
  it('says nothing about a length that can be chosen', () => {
    expect(durationProblem(300_000)).toBeNull();
    expect(durationProblem(225_000)).toBeNull();
  });

  it('is honest that longer videos are not supported yet, and gives the range', () => {
    expect(durationProblem(1_860_000)).toBe(
      'Videos longer than 30:00 are not supported yet. Enter a length from 1:00 to 30:00.',
    );
    expect(durationProblem(3_600_000)).toMatch(/longer than 30:00 are not supported yet/);
  });

  it('says what to enter when the text was not a length, or was too short', () => {
    expect(durationProblem(null)).toBe('Enter a length from 1:00 to 30:00, such as 5 or 7:30.');
    expect(durationProblem(30_000)).toMatch(/shorter than 1:00 are not supported/);
  });
});

describe('formatTargetDuration', () => {
  it('writes whole minutes as before and everything else exactly', () => {
    expect(formatTargetDuration(300_000)).toBe('5 minutes');
    expect(formatTargetDuration(60_000)).toBe('1 minute');
    expect(formatTargetDuration(225_000)).toBe('3 min 45 s');
    expect(formatTargetDuration(65_000)).toBe('1 min 05 s');
    expect(formatTargetDuration(45_000)).toBe('45 s');
  });
});
