'use client';

import { ChevronRight, FolderSearch } from 'lucide-react';
import Link from 'next/link';
import { useParams, usePathname } from 'next/navigation';
import type { ReactNode } from 'react';
import { SOURCE_LABEL } from '@/components/projects/project-row';
import { StageBadge } from '@/components/projects/stage-badge';
import { WorkflowStepper } from '@/components/projects/workflow-stepper';
import { ButtonLink } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { EmptyState, ErrorState, Skeleton } from '@/components/ui/feedback';
import { ApiError } from '@/lib/api/errors';
import { useWorkspace } from '@/lib/api/queries';
import { formatDuration } from '@/lib/format';
import { languageLabel, orientationLabel } from '@/lib/generation-settings';
import type { StepId } from '@/lib/workflow';
import { WorkspaceProvider } from './workspace-context';

function viewingStep(pathname: string): StepId {
  if (pathname.endsWith('/topics')) return 'topics';
  if (pathname.endsWith('/script')) return 'script';
  return 'source';
}

function WorkspaceSkeleton() {
  return (
    <div className="space-y-6" aria-busy="true" aria-label="Loading project">
      <div className="space-y-3">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-9 w-96 max-w-full" />
      </div>
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-64 w-full" />
    </div>
  );
}

/**
 * The frame around every project screen: breadcrumb, title, stage, settings at a glance and the
 * Source → Topics → Script → Approved stepper. It loads the project once and shares it (and the
 * workflow worked out from it) with whichever page is showing.
 */
export function WorkspaceShell({ children }: { children: ReactNode }) {
  const { projectId } = useParams<{ projectId: string }>();
  const pathname = usePathname();
  const workspace = useWorkspace(projectId);
  const { project, workflow } = workspace;

  if (workspace.isLoading) return <WorkspaceSkeleton />;

  if (!project || !workflow) {
    const missing = workspace.error instanceof ApiError && workspace.error.kind === 'not_found';
    return missing ? (
      <EmptyState
        icon={FolderSearch}
        title="We couldn't find that project"
        description="It may have been deleted, or it may belong to a different account."
        action={<ButtonLink href="/projects">Back to projects</ButtonLink>}
      />
    ) : (
      <ErrorState
        error={workspace.error}
        action="load this project"
        onRetry={() => window.location.reload()}
      />
    );
  }

  return (
    <WorkspaceProvider value={{ ...workspace, project, workflow }}>
      <div className="space-y-8">
        <header className="space-y-4">
          <nav aria-label="Breadcrumb" className="flex items-center gap-1.5 text-sm text-ink-500">
            <Link href="/projects" className="rounded hover:text-ink-900 hover:underline">
              Projects
            </Link>
            <ChevronRight className="size-3.5" aria-hidden />
            <span className="truncate text-ink-700" aria-current="page">
              {project.title}
            </span>
          </nav>
          <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
            <div className="min-w-0">
              <h1 className="text-[28px] leading-9 font-bold tracking-tight text-ink-950 break-anywhere">
                {project.title}
              </h1>
              <p className="mt-1.5 text-sm text-ink-500">
                {SOURCE_LABEL[project.source_type]} · {orientationLabel(project.orientation)} ·{' '}
                {formatDuration(project.target_duration_ms)} · {languageLabel(project.language)}
              </p>
            </div>
            <StageBadge stage={workflow.stage} />
          </div>
        </header>

        <Card className="px-4 py-5 sm:px-8">
          <WorkflowStepper
            projectId={project.id}
            steps={workflow.steps}
            viewing={viewingStep(pathname)}
          />
        </Card>

        {children}
      </div>
    </WorkspaceProvider>
  );
}
