'use client';

import { CheckCircle2, CircleDashed, EyeOff, SearchX } from 'lucide-react';
import type { KeywordCoverageItem } from '@edutoon/contracts';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Alert } from '@/components/ui/feedback';
import { useKeywordCoverage, useUpdateTopic } from '@/lib/api/queries';
import type { Tone } from '@/lib/workflow';

const STATE: Record<KeywordCoverageItem['state'], { label: string; tone: Tone }> = {
  covered: { label: 'Covered by your document', tone: 'success' },
  not_in_document: { label: 'Not in your document', tone: 'danger' },
  not_checked: { label: 'Not checked yet', tone: 'neutral' },
  left_out: { label: 'Left out', tone: 'neutral' },
};

function StateIcon({ state }: { state: KeywordCoverageItem['state'] }) {
  const className = 'mt-0.5 size-4 shrink-0';
  if (state === 'covered') return <CheckCircle2 className={`${className} text-success-600`} />;
  if (state === 'not_in_document') return <SearchX className={`${className} text-danger-600`} />;
  if (state === 'left_out') return <EyeOff className={`${className} text-ink-400`} />;
  return <CircleDashed className={`${className} text-ink-400`} />;
}

/**
 * Whether the person's own document backs each keyword they typed. A keyword the document says
 * nothing about is shown as exactly that, with the choice to leave it out - it is never presented
 * as covered, and nothing about it can reach the video as something the document says.
 */
export function KeywordCoverageCard({ projectId }: { projectId: string }) {
  const coverage = useKeywordCoverage(projectId);
  const update = useUpdateTopic(projectId);

  const report = coverage.data;
  if (!report || report.keywords.length === 0) return null;

  const missing = report.keywords.filter((item) => item.state === 'not_in_document');

  return (
    <Card>
      <CardHeader
        title="Your keywords and your document"
        description={report.summary ?? undefined}
        icon={<CheckCircle2 className="size-4" aria-hidden />}
      />
      <CardBody className="space-y-4">
        {missing.length > 0 ? (
          <Alert tone="warning" title="Some keywords are not in your document">
            The lesson is taught only from what your document says, so{' '}
            {missing.map((item) => `"${item.title}"`).join(', ')}{' '}
            {missing.length === 1 ? 'cannot' : 'cannot'} be taught from it. Leave{' '}
            {missing.length === 1 ? 'it' : 'them'} out below, or add material that covers{' '}
            {missing.length === 1 ? 'it' : 'them'}. Nothing was invented to fill the gap.
          </Alert>
        ) : null}
        <ul className="divide-y divide-line" aria-label="Keyword coverage">
          {report.keywords.map((item) => (
            <li key={item.topic_id} className="flex flex-wrap items-start gap-x-3 gap-y-2 py-3">
              <StateIcon state={item.state} />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-semibold text-ink-950">{item.title}</p>
                <p className="text-[13px] text-ink-600">{item.message}</p>
              </div>
              <Badge tone={STATE[item.state].tone} dot>
                {STATE[item.state].label}
              </Badge>
              {item.state === 'not_in_document' ? (
                <Button
                  size="sm"
                  variant="secondary"
                  loading={update.isPending && update.variables?.topicId === item.topic_id}
                  onClick={() =>
                    update.mutate({ topicId: item.topic_id, patch: { status: 'rejected' } })
                  }
                >
                  Leave it out
                </Button>
              ) : null}
            </li>
          ))}
        </ul>
      </CardBody>
    </Card>
  );
}
