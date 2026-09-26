'use client';

import { ChevronRight, FileText, ListChecks } from 'lucide-react';
import Link from 'next/link';
import type { Project } from '@edutoon/contracts';
import { Skeleton } from '@/components/ui/feedback';
import { useProjectStage } from '@/lib/api/queries';
import { formatDateTime, formatDuration, formatRelative } from '@/lib/format';
import { languageLabel, orientationLabel } from '@/lib/generation-settings';
import { StageBadge } from './stage-badge';

export const SOURCE_LABEL: Record<Project['source_type'], string> = {
  pdf: 'PDF document',
  topic: 'Keywords',
};

export function ProjectRow({ project }: { project: Project }) {
  const workflow = useProjectStage(project);
  const Icon = project.source_type === 'pdf' ? FileText : ListChecks;

  return (
    <li>
      <Link
        href={`/projects/${project.id}`}
        className="group flex flex-col gap-3 px-4 py-4 transition-colors hover:bg-surface-sunken sm:flex-row sm:items-center sm:gap-4 sm:px-6"
      >
        <span className="flex size-10 shrink-0 items-center justify-center rounded-md bg-brand-50 text-brand-600">
          <Icon className="size-5" aria-hidden />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[15px] font-semibold text-ink-950 group-hover:text-brand-700">
            {project.title}
          </span>
          <span className="mt-0.5 block truncate text-[13px] text-ink-500">
            {project.source_type === 'pdf' ? 'PDF' : 'Keywords'} ·{' '}
            {orientationLabel(project.orientation)} · {formatDuration(project.target_duration_ms)} ·{' '}
            {languageLabel(project.language)}
          </span>
        </span>
        <span className="flex items-center justify-between gap-4 sm:justify-end">
          <StageBadge stage={workflow.stage} />
          <span
            className="w-24 text-right text-[13px] text-ink-500 sm:block"
            title={formatDateTime(project.updated_at)}
          >
            {formatRelative(project.updated_at)}
          </span>
          <ChevronRight
            className="hidden size-4 text-ink-300 transition-transform group-hover:translate-x-0.5 group-hover:text-brand-600 sm:block"
            aria-hidden
          />
        </span>
      </Link>
    </li>
  );
}

export function ProjectRowSkeleton() {
  return (
    <li className="flex items-center gap-4 px-6 py-4" aria-hidden>
      <Skeleton className="size-10" />
      <div className="flex-1 space-y-2">
        <Skeleton className="h-4 w-1/3" />
        <Skeleton className="h-3 w-1/2" />
      </div>
    </li>
  );
}
