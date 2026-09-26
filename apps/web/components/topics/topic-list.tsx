'use client';

import { BookOpen, Check, ChevronDown, ChevronRight, Sparkles, User, X } from 'lucide-react';
import { useMemo } from 'react';
import type { Topic } from '@edutoon/contracts';
import { cn } from '@/lib/cn';
import { buildTree, evidencePages, visibleRows } from './topic-tree';

interface TopicListProps {
  topics: readonly Topic[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  collapsed: ReadonlySet<string>;
  onToggle: (id: string) => void;
}

/** A small mark for where a topic came from: source pages, the person's own, or an unverified suggestion. */
export function OriginIcon({ topic, className }: { topic: Topic; className?: string }) {
  if (topic.origin === 'suggested') {
    return <Sparkles className={cn('text-warning-600', className)} aria-hidden />;
  }
  if (topic.evidence.length > 0) {
    return <BookOpen className={cn('text-brand-600', className)} aria-hidden />;
  }
  return <User className={cn('text-ink-400', className)} aria-hidden />;
}

function originLabel(topic: Topic): string {
  if (topic.origin === 'suggested') return 'Suggested, not from your source';
  if (topic.evidence.length > 0) return evidencePages(topic) ?? 'Cited from your source';
  return 'Added by you';
}

function StatusMark({ status }: { status: Topic['status'] }) {
  if (status === 'accepted')
    return (
      <span className="flex size-5 items-center justify-center rounded-full bg-success-600 text-white">
        <Check className="size-3" strokeWidth={3} aria-hidden />
        <span className="sr-only">Accepted</span>
      </span>
    );
  if (status === 'rejected')
    return (
      <span className="flex size-5 items-center justify-center rounded-full bg-ink-300 text-white">
        <X className="size-3" strokeWidth={3} aria-hidden />
        <span className="sr-only">Rejected</span>
      </span>
    );
  return (
    <span className="flex size-5 items-center justify-center" title="Needs your review">
      <span className="size-2.5 rounded-full border-2 border-brand-400 bg-surface" aria-hidden />
      <span className="sr-only">Needs review</span>
    </span>
  );
}

/**
 * The topic hierarchy. Each row is a button that selects the topic; a separate button folds a
 * topic's subtopics away, and says which state it is in.
 */
export function TopicList({ topics, selectedId, onSelect, collapsed, onToggle }: TopicListProps) {
  const rows = useMemo(() => visibleRows(buildTree(topics), collapsed), [topics, collapsed]);

  return (
    <ul aria-label="Topics" className="space-y-0.5">
      {rows.map(({ node, depth, expandable, expanded }) => {
        const { topic } = node;
        const selected = topic.id === selectedId;
        return (
          <li
            key={topic.id}
            style={{ paddingLeft: `${depth * 20}px` }}
            className="flex items-stretch gap-1"
          >
            {expandable ? (
              <button
                type="button"
                onClick={() => onToggle(topic.id)}
                aria-expanded={expanded}
                aria-label={`${expanded ? 'Collapse' : 'Expand'} subtopics of ${topic.title}`}
                className="flex w-6 shrink-0 items-center justify-center rounded text-ink-400 hover:bg-canvas hover:text-ink-800"
              >
                {expanded ? (
                  <ChevronDown className="size-4" aria-hidden />
                ) : (
                  <ChevronRight className="size-4" aria-hidden />
                )}
              </button>
            ) : (
              <span className="w-6 shrink-0" aria-hidden />
            )}
            <button
              type="button"
              onClick={() => onSelect(topic.id)}
              aria-current={selected ? 'true' : undefined}
              className={cn(
                'flex min-w-0 flex-1 items-center gap-2.5 rounded-md px-2.5 py-2 text-left transition-colors',
                selected ? 'bg-brand-50 ring-1 ring-brand-200 ring-inset' : 'hover:bg-canvas',
              )}
            >
              <StatusMark status={topic.status} />
              <span className="min-w-0 flex-1">
                <span
                  className={cn(
                    'block truncate text-sm',
                    selected ? 'font-semibold text-brand-900' : 'font-medium text-ink-900',
                    topic.status === 'rejected' && 'text-ink-400 line-through',
                  )}
                >
                  {topic.title}
                </span>
                <span className="mt-0.5 flex items-center gap-1 text-xs text-ink-500">
                  <OriginIcon topic={topic} className="size-3 shrink-0" />
                  <span className="truncate">{originLabel(topic)}</span>
                </span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
