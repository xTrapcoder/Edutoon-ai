'use client';

import { Activity } from 'lucide-react';
import type { Job, JobKind } from '@edutoon/contracts';
import { Badge } from '@/components/ui/badge';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { formatDateTime, formatRelative } from '@/lib/format';
import { jobFailureMessage } from '@/lib/jobs';
import type { Tone } from '@/lib/workflow';

const KIND_LABEL: Record<JobKind, string> = {
  parse_source: 'Reading your PDF',
  build_evidence: 'Preparing your source for verification',
  extract_topics: 'Extracting topics',
  extract_claims: 'Extracting and verifying claims',
  generate_script: 'Writing the script',
  synthesise_voice: 'Narration',
  generate_storyboard: 'Building the storyboard',
  render_video: 'Rendering',
};

const STATUS: Record<Job['status'], { label: string; tone: Tone; busy: boolean }> = {
  queued: { label: 'Queued', tone: 'neutral', busy: true },
  running: { label: 'Running', tone: 'info', busy: true },
  succeeded: { label: 'Done', tone: 'success', busy: false },
  failed: { label: 'Failed', tone: 'danger', busy: false },
  cancelled: { label: 'Cancelled', tone: 'neutral', busy: false },
};

/** A project's recent background work, newest first - including what went wrong, in plain words. */
export function ActivityCard({ jobs }: { jobs: readonly Job[] }) {
  const recent = jobs.slice(0, 6);
  return (
    <Card>
      <CardHeader
        title="Activity"
        description="What has been running for this project."
        icon={<Activity className="size-4" aria-hidden />}
      />
      <CardBody>
        {recent.length === 0 ? (
          <p className="text-sm text-ink-500">
            Nothing has run yet. Activity appears here as you go.
          </p>
        ) : (
          <ul className="divide-y divide-line">
            {recent.map((job) => {
              const status = STATUS[job.status];
              return (
                <li
                  key={job.id}
                  className="flex flex-wrap items-start gap-x-4 gap-y-1 py-3 first:pt-0 last:pb-0"
                >
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-ink-950">{KIND_LABEL[job.kind]}</p>
                    {job.status === 'failed' ? (
                      <p className="mt-0.5 text-[13px] text-danger-700">{jobFailureMessage(job)}</p>
                    ) : null}
                  </div>
                  <Badge tone={status.tone} dot busy={status.busy}>
                    {status.label}
                  </Badge>
                  <time
                    dateTime={job.created_at}
                    title={formatDateTime(job.created_at)}
                    className="w-28 text-right text-[13px] text-ink-500"
                  >
                    {formatRelative(job.created_at)}
                  </time>
                </li>
              );
            })}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}
