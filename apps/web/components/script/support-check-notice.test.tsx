import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ScriptChecks } from '@/lib/jobs';
import { SupportCheckNotice } from './support-check-notice';

const checks = (over: Partial<ScriptChecks> = {}): ScriptChecks => ({
  generalKnowledge: false,
  support: {
    enabled: true,
    judgedSentences: 119,
    uncheckedSentences: 0,
    removed: [],
    fallbackTopics: [],
  },
  shortfall: null,
  ...over,
});

describe('SupportCheckNotice - a script shorter than its target', () => {
  it('says why, in minutes and seconds, and that nothing was added to reach the target', () => {
    render(
      <SupportCheckNotice
        checks={checks({
          shortfall: { estimatedMs: 152_400, targetMs: 300_000, reason: 'evidence_limited' },
        })}
      />,
    );

    expect(
      screen.getByText('This script runs about 2:32, shorter than your 5:00 target'),
    ).toBeInTheDocument();
    expect(screen.getByText(/nothing was added to reach the target/i)).toBeInTheDocument();
    expect(screen.getByText(/the facts verified from your source/i)).toBeInTheDocument();
  });

  it('talks about the topic, not a source, for a general-knowledge lesson', () => {
    render(
      <SupportCheckNotice
        checks={checks({
          generalKnowledge: true,
          support: null,
          shortfall: { estimatedMs: 143_000, targetMs: 240_000, reason: 'evidence_limited' },
        })}
      />,
    );

    expect(screen.getByText(/the facts the lesson can state/i)).toBeInTheDocument();
    expect(screen.getByText(/your topic does not say/i)).toBeInTheDocument();
  });

  it('shows nothing about length when the script is as long as asked', () => {
    render(<SupportCheckNotice checks={checks()} />);

    expect(screen.queryByText(/shorter than your/i)).not.toBeInTheDocument();
  });
});

describe('SupportCheckNotice - a script that is short for another reason', () => {
  it('says the writer left facts unused and that a new version may be longer', () => {
    render(
      <SupportCheckNotice
        checks={checks({
          shortfall: { estimatedMs: 123_000, targetMs: 240_000, reason: 'writer_short' },
        })}
      />,
    );

    expect(
      screen.getByText('This script runs about 2:03, shorter than your 4:00 target'),
    ).toBeInTheDocument();
    expect(screen.getByText(/did not use all of the verified facts/i)).toBeInTheDocument();
    expect(screen.queryByText(/nothing was added to reach the target/i)).not.toBeInTheDocument();
  });
});
