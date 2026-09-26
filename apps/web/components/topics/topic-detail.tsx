'use client';

import { BookOpen, Check, Info, Pencil, RotateCcw, Sparkles, User, X } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import type { Topic, TopicUpdateRequest } from '@edutoon/contracts';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardBody } from '@/components/ui/card';
import { Alert, Skeleton } from '@/components/ui/feedback';
import { Field, Input, Textarea } from '@/components/ui/field';
import { describeError } from '@/lib/api/errors';
import { useSourceChunks, useUpdateTopic } from '@/lib/api/queries';
import { cn } from '@/lib/cn';
import { formatPageRange } from '@/lib/format';
import { evidencePages } from './topic-tree';

// The limits the API enforces by default (see MAX_TOPIC_TITLE_CHARS / MAX_TOPIC_SUMMARY_CHARS).
const TITLE_LIMIT = 120;
const SUMMARY_LIMIT = 400;
const EXCERPT_CHARS = 420;

interface TopicDetailProps {
  projectId: string;
  topic: Topic;
  /** Wording cannot be changed while an extraction runs; accepting and rejecting still can. */
  wordingLocked: boolean;
}

function PassageExcerpt({ projectId, topic }: { projectId: string; topic: Topic }) {
  const first = topic.evidence[0];
  const chunks = useSourceChunks(projectId, first?.source_id);
  const [expanded, setExpanded] = useState(false);

  if (!first) return null;
  if (chunks.isPending) return <Skeleton className="h-16 w-full" />;
  if (chunks.isError) {
    return (
      <p className="text-[13px] text-ink-500">
        We couldn&apos;t load the passage text just now, but the pages above are where this topic
        comes from.
      </p>
    );
  }

  const chunk = chunks.data.find((candidate) => candidate.id === first.source_chunk_id);
  if (!chunk) return null;
  const text = chunk.content.replace(/\s+/g, ' ').trim();
  const long = text.length > EXCERPT_CHARS;
  const shown = expanded || !long ? text : `${text.slice(0, EXCERPT_CHARS).trimEnd()}…`;
  const pages = formatPageRange(first.page_from, first.page_to);

  return (
    <figure className="rounded-md border-l-4 border-brand-300 bg-brand-50/50 p-4">
      <blockquote className="text-sm leading-6 text-ink-700">{shown}</blockquote>
      <figcaption className="mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-ink-500">
        <span>{pages ?? 'From your document'}</span>
        {long ? (
          <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            className="font-semibold text-brand-700 hover:underline"
          >
            {expanded ? 'Show less' : 'Show more'}
          </button>
        ) : null}
      </figcaption>
    </figure>
  );
}

function Evidence({ projectId, topic }: { projectId: string; topic: Topic }) {
  const pages = evidencePages(topic);

  if (topic.origin === 'suggested') {
    return (
      <Alert tone="warning" title="Suggested, and not verified">
        This topic was proposed to round out your outline. It is not drawn from your source
        material, so nothing about it has been checked against a source.
      </Alert>
    );
  }
  if (topic.evidence.length === 0) {
    return (
      <p className="flex gap-2 text-sm text-ink-600">
        <User className="mt-0.5 size-4 shrink-0 text-ink-400" aria-hidden />
        You added this topic yourself, so it does not point to a page in your source.
      </p>
    );
  }
  return (
    <div className="space-y-3">
      <p className="flex items-center gap-2 text-sm text-ink-700">
        <BookOpen className="size-4 shrink-0 text-brand-600" aria-hidden />
        Drawn from <span className="font-semibold text-ink-950">{pages ?? 'your document'}</span>
        {topic.evidence.length > 1 ? (
          <span className="text-ink-500">({topic.evidence.length} passages)</span>
        ) : null}
      </p>
      <PassageExcerpt projectId={projectId} topic={topic} />
    </div>
  );
}

function EditWording({
  projectId,
  topic,
  onDone,
}: {
  projectId: string;
  topic: Topic;
  onDone: () => void;
}) {
  const [title, setTitle] = useState(topic.title);
  const [summary, setSummary] = useState(topic.summary ?? '');
  const update = useUpdateTopic(projectId);

  const trimmedTitle = title.trim();
  const trimmedSummary = summary.trim();
  const titleError = trimmedTitle === '' ? 'A topic needs a title.' : null;
  const patch: TopicUpdateRequest = {};
  if (trimmedTitle !== topic.title) patch.title = trimmedTitle;
  if (trimmedSummary !== (topic.summary ?? ''))
    patch.summary = trimmedSummary === '' ? null : trimmedSummary;
  const changed = Object.keys(patch).length > 0;

  const save = () => {
    if (titleError || !changed) return;
    update.mutate(
      { topicId: topic.id, patch },
      {
        onSuccess: () => {
          toast.success('Topic updated');
          onDone();
        },
      },
    );
  };

  const copy = update.isError ? describeError(update.error, { action: 'save that wording' }) : null;

  return (
    <div className="space-y-4 rounded-lg border border-line bg-surface-sunken p-4">
      <Field label="Title" required error={titleError}>
        {(control) => (
          <Input
            {...control}
            value={title}
            maxLength={TITLE_LIMIT}
            onChange={(event) => setTitle(event.target.value)}
          />
        )}
      </Field>
      <Field
        label="Summary"
        hint={`${summary.length} of ${SUMMARY_LIMIT} characters. A label for review, not a verified claim.`}
      >
        {(control) => (
          <Textarea
            {...control}
            value={summary}
            maxLength={SUMMARY_LIMIT}
            rows={4}
            onChange={(event) => setSummary(event.target.value)}
          />
        )}
      </Field>
      {copy ? (
        <Alert tone="danger" title={copy.title}>
          {copy.message}
        </Alert>
      ) : null}
      <div className="flex justify-end gap-3">
        <Button variant="secondary" onClick={onDone} disabled={update.isPending}>
          Cancel
        </Button>
        <Button
          onClick={save}
          loading={update.isPending}
          disabled={!changed || titleError !== null}
        >
          Save wording
        </Button>
      </div>
    </div>
  );
}

const DECISIONS = [
  {
    status: 'accepted',
    label: 'Accept',
    icon: Check,
    active: 'border-success-600 bg-success-50 text-success-700',
  },
  {
    status: 'proposed',
    label: 'Needs review',
    icon: RotateCcw,
    active: 'border-brand-500 bg-brand-50 text-brand-700',
  },
  {
    status: 'rejected',
    label: 'Reject',
    icon: X,
    active: 'border-danger-600 bg-danger-50 text-danger-700',
  },
] as const;

/** The selected topic: its wording, where it came from and the accept / reject decision. */
export function TopicDetail({ projectId, topic, wordingLocked }: TopicDetailProps) {
  const [editing, setEditing] = useState(false);
  const update = useUpdateTopic(projectId);

  const decide = (status: Topic['status']) => {
    if (status === topic.status) return;
    update.mutate(
      { topicId: topic.id, patch: { status } },
      {
        onError: (error) => {
          const copy = describeError(error, { action: 'update that topic' });
          toast.error(copy.title, { description: copy.message });
        },
      },
    );
  };

  return (
    <Card>
      <CardBody className="space-y-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-xl font-semibold text-ink-950 break-anywhere">{topic.title}</h2>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <Badge
                tone={
                  topic.origin === 'suggested'
                    ? 'warning'
                    : topic.evidence.length > 0
                      ? 'info'
                      : 'neutral'
                }
              >
                {topic.origin === 'suggested' ? (
                  <Sparkles className="size-3" aria-hidden />
                ) : topic.evidence.length > 0 ? (
                  <BookOpen className="size-3" aria-hidden />
                ) : (
                  <User className="size-3" aria-hidden />
                )}
                {topic.origin === 'suggested'
                  ? 'Suggested'
                  : topic.evidence.length > 0
                    ? 'From your source'
                    : 'Added by you'}
              </Badge>
            </div>
          </div>
          {!editing ? (
            <Button
              size="sm"
              variant="secondary"
              onClick={() => setEditing(true)}
              disabled={wordingLocked}
              title={
                wordingLocked
                  ? 'Wording cannot be changed while topics are being extracted'
                  : undefined
              }
              leadingIcon={<Pencil className="size-3.5" aria-hidden />}
            >
              Edit wording
            </Button>
          ) : null}
        </div>

        {editing ? (
          <EditWording projectId={projectId} topic={topic} onDone={() => setEditing(false)} />
        ) : (
          <>
            {topic.summary ? (
              <p className="text-[15px] leading-7 text-ink-700">{topic.summary}</p>
            ) : (
              <p className="text-sm text-ink-500">This topic has no summary.</p>
            )}
            {topic.keywords.length > 0 ? (
              <ul className="flex flex-wrap gap-1.5" aria-label="Key terms">
                {topic.keywords.map((keyword) => (
                  <li
                    key={keyword}
                    className="rounded-full bg-brand-50 px-2.5 py-0.5 text-[13px] font-medium text-brand-800"
                  >
                    {keyword}
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        )}

        <section aria-labelledby="evidence-heading" className="space-y-3 border-t border-line pt-5">
          <h3 id="evidence-heading" className="text-sm font-semibold text-ink-950">
            Evidence and source
          </h3>
          <Evidence projectId={projectId} topic={topic} />
        </section>

        <section aria-labelledby="decision-heading" className="space-y-3 border-t border-line pt-5">
          <h3 id="decision-heading" className="text-sm font-semibold text-ink-950">
            Your decision
          </h3>
          <div role="group" aria-labelledby="decision-heading" className="grid grid-cols-3 gap-2">
            {DECISIONS.map((decision) => {
              const active = topic.status === decision.status;
              return (
                <button
                  key={decision.status}
                  type="button"
                  aria-pressed={active}
                  onClick={() => decide(decision.status)}
                  className={cn(
                    'flex h-10 items-center justify-center gap-1.5 rounded-md border text-sm font-semibold transition-colors',
                    active
                      ? decision.active
                      : 'border-line-strong bg-surface text-ink-600 hover:border-ink-300 hover:text-ink-950',
                  )}
                >
                  <decision.icon className="size-4" aria-hidden />
                  {decision.label}
                </button>
              );
            })}
          </div>
          <p className="flex gap-2 text-[13px] text-ink-500">
            <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            Your decision is saved with the project. Rejected topics stay in the list, greyed out,
            so you can change your mind.
          </p>
        </section>
      </CardBody>
    </Card>
  );
}
