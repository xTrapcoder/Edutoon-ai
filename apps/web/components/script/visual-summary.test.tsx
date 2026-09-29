import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { IllustrationSummary, visualOf, visualWasDropped } from './visual-summary';

const flow = {
  illustration: {
    description: 'How a leaf works',
    visual: {
      type: 'process_flow',
      title: 'Sugar in a leaf',
      nodes: [{ label: 'Light arrives', detail: 'on the blade' }, { label: 'Sugar is built' }],
    },
  },
};

describe('visualOf / visualWasDropped', () => {
  it('finds a known visual and ignores everything else', () => {
    expect(visualOf(flow)?.type).toBe('process_flow');
    expect(visualOf({ illustration: { visual: { type: 'value_chart' } } })).toBeUndefined();
    expect(visualOf({ illustration: { visual: 'a string' } })).toBeUndefined();
    expect(visualOf({ illustration: { visual: [] } })).toBeUndefined();
    expect(visualOf({ illustration: null })).toBeUndefined();
    expect(visualOf(null)).toBeUndefined();
    expect(visualOf('nonsense')).toBeUndefined();
  });

  it('reads the dropped mark only when it is exactly true', () => {
    expect(visualWasDropped({ illustration: { visual_dropped: true } })).toBe(true);
    expect(visualWasDropped({ illustration: { visual_dropped: 'yes' } })).toBe(false);
    expect(visualWasDropped({ illustration: {} })).toBe(false);
    expect(visualWasDropped(undefined)).toBe(false);
  });
});

describe('IllustrationSummary', () => {
  it('summarises a process flow as numbered steps with their detail', () => {
    render(<IllustrationSummary content={flow} />);
    expect(screen.getByText('Process flow')).toBeInTheDocument();
    expect(screen.getByText(/Sugar in a leaf/)).toBeInTheDocument();
    const steps = screen.getAllByRole('listitem');
    expect(steps).toHaveLength(2);
    expect(steps[0]).toHaveTextContent('Light arrives — on the blade');
  });

  it('marks a cycle as returning to its first step', () => {
    render(
      <IllustrationSummary
        content={{
          illustration: {
            visual: {
              type: 'cycle',
              nodes: [{ label: 'Rain' }, { label: 'Rivers' }, { label: 'Sea' }],
            },
          },
        }}
      />,
    );
    expect(screen.getByText('Cycle')).toBeInTheDocument();
    expect(screen.getByText(/then back to the first step/)).toBeInTheDocument();
  });

  it('indents a hierarchy by depth, following parent indices', () => {
    render(
      <IllustrationSummary
        content={{
          illustration: {
            visual: {
              type: 'hierarchy',
              nodes: [
                { label: 'Plants' },
                { label: 'Leaves', parent: 0 },
                { label: 'Stomata', parent: 1 },
              ],
            },
          },
        }}
      />,
    );
    expect(screen.getByText('Plants')).toHaveStyle({ paddingLeft: '0px' });
    expect(screen.getByText('Leaves')).toHaveStyle({ paddingLeft: '16px' });
    expect(screen.getByText('Stomata')).toHaveStyle({ paddingLeft: '32px' });
  });

  it('cannot loop forever on a malformed hierarchy', () => {
    render(
      <IllustrationSummary
        content={{
          illustration: {
            visual: {
              type: 'hierarchy',
              nodes: [
                { label: 'A', parent: 1 },
                { label: 'B', parent: 0 },
              ],
            },
          },
        }}
      />,
    );
    expect(screen.getByText('A')).toBeInTheDocument();
    expect(screen.getByText('B')).toBeInTheDocument();
  });

  it('shows a comparison as columns that stack on a narrow screen', () => {
    const { container } = render(
      <IllustrationSummary
        content={{
          illustration: {
            visual: {
              type: 'comparison',
              relation: 'versus',
              columns: [
                { heading: 'Light', points: ['Grows', 'Green'] },
                { heading: 'Dark', points: ['Weakens'] },
              ],
            },
          },
        }}
      />,
    );
    expect(screen.getByText('Comparison')).toBeInTheDocument();
    expect(screen.getByText('Light')).toBeInTheDocument();
    expect(screen.getByText('Weakens')).toBeInTheDocument();
    // One column below the small breakpoint, two above it - never a fixed two-column grid.
    const grid = container.querySelector('.grid');
    expect(grid?.className).toContain('grid-cols-1');
    expect(grid?.className).toContain('sm:grid-cols-2');
  });

  it('frames labelled components honestly as labelled parts', () => {
    render(
      <IllustrationSummary
        content={{
          illustration: {
            visual: {
              type: 'labelled_components',
              form: 'hub',
              subject: 'A leaf',
              parts: [{ label: 'Blade' }, { label: 'Veins', detail: 'carry water' }],
            },
          },
        }}
      />,
    );
    expect(screen.getByText('Labelled parts')).toBeInTheDocument();
    expect(screen.getByText('A leaf')).toBeInTheDocument();
    expect(screen.getByText(/Veins/)).toBeInTheDocument();
  });

  it('lets long words wrap instead of overflowing a narrow card', () => {
    const long = 'Internationalisation'.repeat(4);
    const { container } = render(
      <IllustrationSummary
        content={{
          illustration: {
            visual: { type: 'process_flow', nodes: [{ label: long }, { label: 'Next' }] },
          },
        }}
      />,
    );
    const item = screen.getByText(long).closest('li');
    expect(item?.className).toContain('break-words');
    expect(container.querySelectorAll('[class*="whitespace-nowrap"]')).toHaveLength(0);
  });

  it('says a scene with no visual is drawn as text, and never promises a picture', () => {
    render(
      <IllustrationSummary
        content={{ illustration: { description: 'A server room' } }}
        description="A server room"
      />,
    );
    expect(screen.getByText('Shown as text — no visual')).toBeInTheDocument();
    expect(screen.getByText(/A server room/)).toBeInTheDocument();
    expect(screen.queryByText(/pending/i)).toBeNull();
  });

  it('says a dropped visual was invalid, without showing its specification', () => {
    render(
      <IllustrationSummary
        content={{ illustration: { visual_dropped: true, description: 'A loop' } }}
      />,
    );
    expect(screen.getByText('Shown as text — its visual was invalid')).toBeInTheDocument();
    expect(screen.queryByText(/nodes|"type"/)).toBeNull();
  });

  it('treats an unknown visual kind as text rather than crashing', () => {
    render(
      <IllustrationSummary
        content={{ illustration: { visual: { type: 'value_chart', values: [1, 2, 3] } } }}
      />,
    );
    expect(screen.getByText('Shown as text — no visual')).toBeInTheDocument();
  });
});
