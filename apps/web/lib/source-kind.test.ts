import { describe, expect, it } from 'vitest';
import { job, project, source, topic } from '@/test/fixtures';
import { formatPageRange } from './format';
import { derivePipeline } from './pipeline';
import { isDeckProject, isFileProject, sourceWords } from './source-kind';
import { deriveWorkflow } from './workflow';

describe('what a project is built from', () => {
  it('treats a PDF and a PowerPoint deck as file projects and keywords alone as neither', () => {
    expect(isFileProject({ source_type: 'pdf' })).toBe(true);
    expect(isFileProject({ source_type: 'pptx' })).toBe(true);
    expect(isFileProject({ source_type: 'topic' })).toBe(false);
    expect(isDeckProject({ source_type: 'pptx' })).toBe(true);
    expect(isDeckProject({ source_type: 'pdf' })).toBe(false);
  });

  it("calls a deck's parts slides and a PDF's pages", () => {
    expect(sourceWords({ source_type: 'pptx' })).toMatchObject({ unit: 'slide', Unit: 'Slide' });
    expect(sourceWords({ source_type: 'pdf' })).toMatchObject({ unit: 'page', Unit: 'Page' });
    expect(formatPageRange(4, 4, 'Slide')).toBe('Slide 4');
    expect(formatPageRange(4, 6, 'Slide')).toBe('Slides 4–6');
    expect(formatPageRange(4, 6)).toBe('Pages 4–6');
  });
});

describe('a PowerPoint project through the pipeline', () => {
  const deck = project({ source_type: 'pptx' });
  const stages = (sources: ReturnType<typeof source>[], extra = {}) => {
    const input = {
      project: deck,
      sources,
      topics: [topic({ status: 'accepted' })],
      jobs: [job({ kind: 'extract_topics' })],
      scripts: [],
      claims: [],
      ...extra,
    };
    return derivePipeline({ ...input, workflow: deriveWorkflow(input) });
  };

  it('waits for a presentation, then reports its slides - not "keywords unsupported"', () => {
    const waiting = stages([])[0];
    expect(waiting).toMatchObject({
      id: 'source',
      status: 'pending',
      detail: 'Add a presentation',
    });

    const read = stages([source({ original_filename: 'induction.pptx', page_count: 12 })])[0];
    expect(read).toMatchObject({
      status: 'completed',
      detail: 'induction.pptx · 12 slides',
    });
  });

  it('carries the same topic stage as a PDF once the deck is read', () => {
    const topicsStage = stages([source({ page_count: 3 })])[2];
    expect(topicsStage).toMatchObject({ id: 'topics', status: 'completed' });
  });
});
