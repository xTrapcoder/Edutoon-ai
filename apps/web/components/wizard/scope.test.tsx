import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { OFFERED_MODES, INSTRUCTIONS_OFFERED } from '@/lib/scope';
import { renderWithClient } from '@/test/render';
import { CreateProjectWizard } from './create-project-wizard';
import { isModeAvailable, usesInstructions } from './wizard-model';

// No scope mock here: these hold the product as shipped. Since 2026-10-18 it offers exactly three
// ways to make a video - a PDF, pasted text, and a PDF with keywords that focus the lesson.
describe('The offered scope', () => {
  it('is exactly PDF, pasted text and PDF + keywords', () => {
    expect([...OFFERED_MODES].sort()).toEqual(['both', 'pdf', 'text']);
    expect(INSTRUCTIONS_OFFERED).toBe(false);
    for (const mode of ['pdf', 'text', 'both'] as const) expect(isModeAvailable(mode)).toBe(true);
    for (const mode of ['pptx', 'keywords'] as const) expect(isModeAvailable(mode)).toBe(false);
    for (const mode of ['pdf', 'text', 'both'] as const) expect(usesInstructions(mode)).toBe(false);
  });

  it('shows three source cards and no other mode', () => {
    renderWithClient(<CreateProjectWizard />);

    const cards = screen.getAllByRole('radio').map((radio) => (radio as HTMLInputElement).value);
    expect(cards).toEqual(['pdf', 'text', 'both']);
    expect(screen.getByRole('radio', { name: /^PDF document/ })).toBeEnabled();
    expect(screen.getByRole('radio', { name: /^Paste text/ })).toBeEnabled();
    expect(screen.getByRole('radio', { name: /^PDF \+ Keywords/ })).toBeEnabled();
    expect(screen.queryByRole('radio', { name: /PowerPoint/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('radio', { name: /Topic only/ })).not.toBeInTheDocument();
  });

  it('describes the keyword mode as a focused video and offers no instructions box', () => {
    renderWithClient(<CreateProjectWizard />);

    expect(screen.getByText(/a focused video on just those topics/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/instructions/i)).not.toBeInTheDocument();
  });
});
