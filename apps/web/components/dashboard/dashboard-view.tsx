'use client';

import { useUser } from '@clerk/nextjs';
import { ArrowRight, FilePlus2, FolderPlus, ListChecks, PenLine, ShieldCheck } from 'lucide-react';
import Link from 'next/link';
import { ProjectRow, ProjectRowSkeleton } from '@/components/projects/project-row';
import { ButtonLink } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { EmptyState, ErrorState } from '@/components/ui/feedback';
import { PageHeader } from '@/components/ui/page-header';
import { useProjectList } from '@/lib/api/queries';
import { useGreeting } from '@/lib/use-greeting';

const HOW_IT_WORKS = [
  {
    icon: FilePlus2,
    title: 'Add your source',
    body: 'Upload a PDF or paste your text. With a PDF you can also add keywords for the topics you want covered.',
  },
  {
    icon: ListChecks,
    title: 'Review the topics',
    body: 'See the topics we found, each with the pages or slides it came from.',
  },
  {
    icon: PenLine,
    title: 'Review the script',
    body: 'Read a script written only from claims checked against your source.',
  },
  {
    icon: ShieldCheck,
    title: 'Approve it',
    body: 'Approve the wording when you are happy. After approval, the script can be narrated, storyboarded, and rendered into video.',
  },
] as const;

export function DashboardView() {
  const { user } = useUser();
  const greeting = useGreeting();
  const list = useProjectList();

  const projects = list.data?.pages.flatMap((page) => page.items) ?? [];
  const recent = projects.slice(0, 5);
  const firstName = user?.firstName;

  return (
    <div className="space-y-8">
      <PageHeader
        title={firstName ? `${greeting}, ${firstName}` : greeting}
        description="Turn your learning material into a verified, cited script - every claim traced back to your source."
        actions={
          <ButtonLink
            href="/projects/new"
            size="lg"
            leadingIcon={<FolderPlus className="size-5" aria-hidden />}
          >
            Create project
          </ButtonLink>
        }
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1.9fr)_minmax(0,1fr)]">
        <Card className="overflow-hidden">
          <CardHeader
            title="Recent projects"
            action={
              projects.length > 0 ? (
                <Link
                  href="/projects"
                  className="inline-flex items-center gap-1 text-sm font-semibold text-brand-700 hover:text-brand-800"
                >
                  View all
                  <ArrowRight className="size-4" aria-hidden />
                </Link>
              ) : null
            }
          />
          <div className="mt-4">
            {list.isPending ? (
              <ul
                className="divide-y divide-line border-t border-line"
                aria-label="Loading projects"
              >
                {[0, 1, 2].map((key) => (
                  <ProjectRowSkeleton key={key} />
                ))}
              </ul>
            ) : list.isError ? (
              <div className="border-t border-line p-6">
                <ErrorState
                  error={list.error}
                  action="load your projects"
                  onRetry={() => void list.refetch()}
                  retrying={list.isRefetching}
                />
              </div>
            ) : recent.length === 0 ? (
              <div className="border-t border-line p-6">
                <EmptyState
                  className="border-0 bg-transparent py-8"
                  icon={FolderPlus}
                  title="Create your first project"
                  description="Upload a PDF or paste your text. We will find what is worth covering and check every claim against your source."
                  action={<ButtonLink href="/projects/new">Create project</ButtonLink>}
                />
              </div>
            ) : (
              <ul
                className="divide-y divide-line border-t border-line"
                aria-label="Recent projects"
              >
                {recent.map((project) => (
                  <ProjectRow key={project.id} project={project} />
                ))}
              </ul>
            )}
          </div>
        </Card>

        <div className="space-y-6">
          <Card>
            <CardHeader title="How EduToon works" />
            <CardBody>
              <ol className="space-y-5">
                {HOW_IT_WORKS.map((step, index) => (
                  <li key={step.title} className="flex gap-3">
                    <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-brand-50 text-brand-700">
                      <step.icon className="size-4" aria-hidden />
                    </span>
                    <div className="min-w-0">
                      <p className="text-sm font-semibold text-ink-950">
                        <span className="text-ink-400">{index + 1}.</span> {step.title}
                      </p>
                      <p className="mt-0.5 text-[13px] text-ink-500">{step.body}</p>
                    </div>
                  </li>
                ))}
              </ol>
            </CardBody>
          </Card>
          <div className="rounded-lg bg-brand-950 p-5 text-white">
            <p className="text-sm font-semibold">Every claim is traceable</p>
            <p className="mt-1.5 text-[13px] leading-5 text-brand-200">
              Nothing reaches a script unless it has been checked against your source material, so
              you can see where each statement came from.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
