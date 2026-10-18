import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { OFFERED_MODES, INSTRUCTIONS_OFFERED } from '@/lib/scope';
import { renderWithClient } from '@/test/render';
import { CreateProjectWizard } from './create-project-wizard';
import { isModeAvailable, usesInstructions } from './wizard-model';

// No scope mock here: these hold the product as shipped. Since 2026-10-18 it offers exactly five
// ways to make a video: a PDF, pasted text, a PDF with keywords, a PDF with instructions, and a
// scanned PDF read by OCR. A topic with no document is not among them.
describe('The offered scope', () => {
  it('is exactly the five modes', () => {
    expect([...OFFERED_MODES].sort()).toEqual(['both', 'instructions', 'pdf', 'scan', 'text']);
    expect(INSTRUCTIONS_OFFERED).toBe(true);
    for (const mode of ['pdf', 'text', 'both', 'instructions', 'scan'] as const) {
      expect(isModeAvailable(mode)).toBe(true);
    }
    expect(isModeAvailable('pptx')).toBe(false);
    expect(isModeAvailable('keywords' as never)).toBe(false);
    expect(usesInstructions('instructions')).toBe(true);
    for (const mode of ['pdf', 'text', 'both', 'scan'] as const) {
      expect(usesInstructions(mode)).toBe(false);
    }
  });

  it('shows five source cards and no other mode', () => {
    renderWithClient(<CreateProjectWizard />);

    const cards = screen.getAllByRole('radio').map((radio) => (radio as HTMLInputElement).value);
    expect(cards).toEqual(['pdf', 'text', 'both', 'instructions', 'scan']);
    expect(screen.getByRole('radio', { name: /^PDF document/ })).toBeEnabled();
    expect(screen.getByRole('radio', { name: /^Paste text/ })).toBeEnabled();
    expect(screen.getByRole('radio', { name: /^PDF \+ Keywords/ })).toBeEnabled();
    expect(screen.getByRole('radio', { name: /^PDF \+ Instructions/ })).toBeEnabled();
    expect(screen.getByRole('radio', { name: /^Scanned PDF/ })).toBeEnabled();
    expect(screen.queryByRole('radio', { name: /PowerPoint/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('radio', { name: /Topic only/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/general knowledge/i)).not.toBeInTheDocument();
  });

  it('shows the instructions box only for PDF + instructions, and says how scans are read', async () => {
    renderWithClient(<CreateProjectWizard />);

    expect(screen.queryByLabelText(/^Instructions/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('radio', { name: /^PDF \+ Instructions/ }));
    expect(screen.getByLabelText(/^Instructions/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('radio', { name: /^Scanned PDF/ }));
    expect(screen.queryByLabelText(/^Instructions/)).not.toBeInTheDocument();
    expect(screen.getByText(/read by OCR on this machine/)).toBeInTheDocument();
  });
});
