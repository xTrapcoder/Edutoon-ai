import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import {
  GenerationSettingsFields,
  type GenerationSettingsValue,
} from './generation-settings-fields';

function Harness({ initial = 300_000 }: { initial?: number }) {
  const [value, setValue] = useState<GenerationSettingsValue>({
    orientation: 'horizontal',
    durationMs: initial,
    language: 'en-IN',
  });
  return (
    <>
      <GenerationSettingsFields value={value} onChange={setValue} />
      <output aria-label="chosen">{value.durationMs}</output>
    </>
  );
}

const box = () => screen.getByLabelText('Target duration, minutes and seconds');
const chosen = () => screen.getByLabelText('chosen').textContent;

describe('the target duration control', () => {
  it('says what is supported, including that longer videos are not', () => {
    render(<Harness />);

    expect(screen.getByText(/any length from 1:00 to 30:00, to the second/i)).toBeInTheDocument();
    expect(
      screen.getByText('Videos longer than 30:00 are not supported yet.', { exact: false }),
    ).toBeInTheDocument();
    expect(screen.getByText('5 minutes')).toBeInTheDocument();
  });

  it('offers a slider limited to the supported range, one second at a time', () => {
    render(<Harness />);

    const slider = screen.getByLabelText('Target duration');
    expect(slider).toHaveAttribute('min', '60000');
    expect(slider).toHaveAttribute('max', '1800000');
    expect(slider).toHaveAttribute('step', '1000');
    expect(slider).toHaveValue('300000');
  });

  it('accepts any length to the second - not a preset', async () => {
    render(<Harness />);

    await userEvent.clear(box());
    await userEvent.type(box(), '3:45');

    expect(chosen()).toBe('225000');
    expect(screen.getByText('3 min 45 s')).toBeInTheDocument();
    expect(screen.getByLabelText('Target duration')).toHaveValue('225000');
  });

  it('reads a bare number as minutes', async () => {
    render(<Harness />);

    await userEvent.clear(box());
    await userEvent.type(box(), '8');

    expect(chosen()).toBe('480000');
  });

  it('refuses a length over the ceiling, says why, and keeps the last good value', async () => {
    render(<Harness />);

    await userEvent.clear(box());
    await userEvent.type(box(), '32:00');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Videos longer than 30:00 are not supported yet. Enter a length from 1:00 to 30:00.',
    );
    expect(box()).toHaveAttribute('aria-invalid', 'true');
    expect(chosen()).not.toBe('1920000');

    await userEvent.tab(); // leaving the box puts the last good value back: the 3 typed first
    expect(box()).toHaveValue('3:00');
  });

  it('refuses a length that is not one, in words', async () => {
    render(<Harness />);

    await userEvent.clear(box());
    await userEvent.type(box(), 'five');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Enter a length from 1:00 to 30:00, such as 5 or 7:30.',
    );
  });

  it('flags a saved target that is already longer than can be made, so it can be shortened', () => {
    render(<Harness initial={2_400_000} />);

    expect(screen.getByRole('alert')).toHaveTextContent(/longer than 30:00 are not supported yet/);
  });

  it('has no fixed-length shortcut buttons', () => {
    render(<Harness />);

    expect(screen.queryByRole('button', { name: /^(1|5|10) ?(min|minutes?)$/i })).toBeNull();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });
});
