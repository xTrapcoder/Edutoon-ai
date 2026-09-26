import type { Topic } from '@edutoon/contracts';

export interface TopicNode {
  topic: Topic;
  children: TopicNode[];
}

/**
 * Nest the API's topics into a tree. The API returns them in reading order (depth first,
 * siblings by position), and that order is kept; a topic whose parent is missing from the list
 * is shown at the top level rather than lost.
 */
export function buildTree(topics: readonly Topic[]): TopicNode[] {
  const nodes = new Map<string, TopicNode>();
  for (const topic of topics) nodes.set(topic.id, { topic, children: [] });

  const roots: TopicNode[] = [];
  for (const topic of topics) {
    const node = nodes.get(topic.id);
    if (!node) continue;
    const parent = topic.parent_id ? nodes.get(topic.parent_id) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}

export interface VisibleRow {
  node: TopicNode;
  depth: number;
  expandable: boolean;
  expanded: boolean;
}

/** The rows to draw, hiding the descendants of anything collapsed. */
export function visibleRows(
  roots: readonly TopicNode[],
  collapsed: ReadonlySet<string>,
): VisibleRow[] {
  const rows: VisibleRow[] = [];
  const walk = (nodes: readonly TopicNode[], depth: number) => {
    for (const node of nodes) {
      const expandable = node.children.length > 0;
      const expanded = expandable && !collapsed.has(node.topic.id);
      rows.push({ node, depth, expandable, expanded });
      if (expanded) walk(node.children, depth + 1);
    }
  };
  walk(roots, 0);
  return rows;
}

export interface TopicCounts {
  total: number;
  accepted: number;
  rejected: number;
  toReview: number;
}

export function countTopics(topics: readonly Topic[]): TopicCounts {
  const accepted = topics.filter((topic) => topic.status === 'accepted').length;
  const rejected = topics.filter((topic) => topic.status === 'rejected').length;
  return {
    total: topics.length,
    accepted,
    rejected,
    toReview: topics.length - accepted - rejected,
  };
}

/** Where a topic was drawn from, in a few words: "Page 4" or "Pages 3–5, 9". */
export function evidencePages(topic: Topic): string | null {
  const ranges: string[] = [];
  for (const entry of topic.evidence) {
    const from = entry.page_from ?? entry.page_to;
    const to = entry.page_to ?? entry.page_from;
    if (from === null || to === null) continue;
    const label = from === to ? String(from) : `${from}–${to}`;
    if (!ranges.includes(label)) ranges.push(label);
  }
  if (ranges.length === 0) return null;
  const single = ranges.length === 1 && !(ranges[0] ?? '').includes('–');
  return `${single ? 'Page' : 'Pages'} ${ranges.join(', ')}`;
}

/** Whether a topic has real source material behind it. */
export const hasEvidence = (topic: Topic): boolean => topic.evidence.length > 0;

/** The topic with this id, or the first one when nothing valid is selected. */
export function findTopic(topics: readonly Topic[], id: string | null): Topic | undefined {
  return id === null ? topics[0] : (topics.find((topic) => topic.id === id) ?? topics[0]);
}
