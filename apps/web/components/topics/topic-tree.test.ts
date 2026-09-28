import { describe, expect, it } from 'vitest';
import { topic } from '@/test/fixtures';
import {
  buildTree,
  countTopics,
  eligibleAcceptedTopics,
  evidencePages,
  visibleRows,
} from './topic-tree';

const evidence = (from: number, to: number) => ({
  source_chunk_id: 'c',
  source_id: 's',
  page_from: from,
  page_to: to,
});

describe('topic tree', () => {
  const topics = [
    topic({ id: 'a', title: 'Models' }),
    topic({ id: 'a1', parent_id: 'a', title: 'OSI', depth: 1 }),
    topic({ id: 'b', title: 'Devices' }),
    topic({ id: 'orphan', parent_id: 'gone', title: 'Lost' }),
  ];

  it('nests children under their parent and keeps reading order', () => {
    const tree = buildTree(topics);
    expect(tree.map((node) => node.topic.id)).toEqual(['a', 'b', 'orphan']);
    expect(tree[0]?.children.map((node) => node.topic.id)).toEqual(['a1']);
  });

  it('shows a topic whose parent is missing rather than losing it', () => {
    expect(buildTree(topics).some((node) => node.topic.id === 'orphan')).toBe(true);
  });

  it('hides the subtopics of anything collapsed', () => {
    const open = visibleRows(buildTree(topics), new Set());
    expect(open.map((row) => row.node.topic.id)).toEqual(['a', 'a1', 'b', 'orphan']);
    const closed = visibleRows(buildTree(topics), new Set(['a']));
    expect(closed.map((row) => row.node.topic.id)).toEqual(['a', 'b', 'orphan']);
  });
});

describe('countTopics', () => {
  it('counts what is left to review', () => {
    const counts = countTopics([
      topic({ status: 'accepted' }),
      topic({ status: 'rejected' }),
      topic({ status: 'proposed' }),
    ]);
    expect(counts).toEqual({ total: 3, accepted: 1, rejected: 1, toReview: 1 });
  });
});

describe('eligibleAcceptedTopics', () => {
  it('counts a topic only when it and every ancestor is accepted', () => {
    const topics = [
      topic({ id: 'a', title: 'Models', status: 'accepted' }),
      topic({ id: 'a1', parent_id: 'a', title: 'OSI', status: 'accepted', depth: 1 }),
      topic({ id: 'b', title: 'Devices', status: 'proposed' }),
      topic({ id: 'b1', parent_id: 'b', title: 'Switches', status: 'accepted', depth: 1 }),
    ];
    // 'b1' is accepted but its parent 'b' is not, so it does not count.
    expect(eligibleAcceptedTopics(topics).map((t) => t.id)).toEqual(['a', 'a1']);
  });

  it('is empty when nothing has been accepted', () => {
    expect(eligibleAcceptedTopics([topic({ status: 'proposed' })])).toEqual([]);
  });
});

describe('evidencePages', () => {
  it('names the pages a topic came from', () => {
    expect(evidencePages(topic({ evidence: [evidence(4, 4)] }))).toBe('Page 4');
    expect(evidencePages(topic({ evidence: [evidence(3, 5), evidence(9, 9)] }))).toBe(
      'Pages 3–5, 9',
    );
    expect(evidencePages(topic({ evidence: [] }))).toBeNull();
  });
});
