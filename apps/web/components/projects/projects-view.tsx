'use client';

import { FolderPlus, Search, SearchX } from 'lucide-react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useEffect, useState } from 'react';
import { Button, ButtonLink } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { EmptyState, ErrorState } from '@/components/ui/feedback';
import { PageHeader } from '@/components/ui/page-header';
import { useProjectList } from '@/lib/api/queries';
import { cn } from '@/lib/cn';
import { pluralise } from '@/lib/format';
import { ProjectRow, ProjectRowSkeleton } from './project-row';

type SourceFilter = 'all' | 'pdf' | 'pptx' | 'topic';

const FILTERS: readonly { value: SourceFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'pdf', label: 'PDF' },
  { value: 'pptx', label: 'PowerPoint' },
  { value: 'topic', label: 'Keywords' },
];

export function ProjectsView() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const urlQuery = searchParams.get('q') ?? '';

  const [query, setQuery] = useState(urlQuery);
  const [filter, setFilter] = useState<SourceFilter>('all');
  const list = useProjectList();
  // Read up front: inside the JSX below TypeScript has already narrowed `list` to its success state.
  const loadMoreFailed = list.isFetchNextPageError;
  const loadError = list.error;
  const loadMore = () => void list.fetchNextPage();

  // The top-bar search lands here with ?q=; keep the box in step with it.
  useEffect(() => {
    setQuery(urlQuery);
  }, [urlQuery]);

  const updateQuery = (value: string) => {
    setQuery(value);
    router.replace(value.trim() ? `${pathname}?q=${encodeURIComponent(value)}` : pathname, {
      scroll: false,
    });
  };

  const projects = list.data?.pages.flatMap((page) => page.items) ?? [];
  const needle = query.trim().toLowerCase();
  const visible = projects.filter(
    (project) =>
      (filter === 'all' || project.source_type === filter) &&
      (needle === '' || project.title.toLowerCase().includes(needle)),
  );
  const filtering = needle !== '' || filter !== 'all';

  return (
    <div className="space-y-6">
      <PageHeader
        title="Projects"
        description="Every project you have started, and where each one has got to."
        actions={
          <ButtonLink
            href="/projects/new"
            leadingIcon={<FolderPlus className="size-4" aria-hidden />}
          >
            Create project
          </ButtonLink>
        }
      />

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative min-w-60 flex-1 sm:max-w-sm">
          <label htmlFor="project-filter" className="sr-only">
            Search your projects by title
          </label>
          <Search
            className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-ink-400"
            aria-hidden
          />
          <input
            id="project-filter"
            type="search"
            value={query}
            onChange={(event) => updateQuery(event.target.value)}
            placeholder="Search by title"
            className="h-10 w-full rounded-md border border-line-strong bg-surface pr-3 pl-9 text-sm text-ink-900 shadow-xs placeholder:text-ink-500 hover:border-ink-300 focus:border-brand-500 focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-brand-500/40"
          />
        </div>
        <div
          role="group"
          aria-label="Filter by source"
          className="inline-flex rounded-md border border-line-strong bg-surface p-0.5 shadow-xs"
        >
          {FILTERS.map((option) => (
            <button
              key={option.value}
              type="button"
              aria-pressed={filter === option.value}
              onClick={() => setFilter(option.value)}
              className={cn(
                'h-9 rounded-[6px] px-3.5 text-sm font-medium transition-colors',
                filter === option.value
                  ? 'bg-brand-50 font-semibold text-brand-700'
                  : 'text-ink-600 hover:text-ink-950',
              )}
            >
              {option.label}
            </button>
          ))}
        </div>
        {!list.isPending && !list.isError ? (
          <p className="ml-auto text-sm text-ink-500" aria-live="polite">
            {filtering
              ? `${pluralise(visible.length, 'project')} shown`
              : `${pluralise(projects.length, 'project')}${list.hasNextPage ? ' so far' : ''}`}
          </p>
        ) : null}
      </div>

      {list.isPending ? (
        <Card className="overflow-hidden">
          <ul className="divide-y divide-line" aria-label="Loading projects">
            {[0, 1, 2, 3].map((key) => (
              <ProjectRowSkeleton key={key} />
            ))}
          </ul>
        </Card>
      ) : list.isError ? (
        <ErrorState
          error={list.error}
          action="load your projects"
          onRetry={() => void list.refetch()}
          retrying={list.isRefetching}
        />
      ) : projects.length === 0 ? (
        <EmptyState
          icon={FolderPlus}
          title="No projects yet"
          description="Create a project from a PDF or a list of topics, and it will appear here."
          action={<ButtonLink href="/projects/new">Create your first project</ButtonLink>}
        />
      ) : visible.length === 0 ? (
        <EmptyState
          icon={SearchX}
          title="No projects match"
          description={
            list.hasNextPage
              ? 'Nothing loaded so far matches. Load more projects to search further back.'
              : 'Try a different title, or clear the filter.'
          }
          action={
            <div className="flex flex-wrap justify-center gap-3">
              <Button
                variant="secondary"
                onClick={() => {
                  updateQuery('');
                  setFilter('all');
                }}
              >
                Clear search and filter
              </Button>
              {list.hasNextPage ? (
                <Button variant="secondary" onClick={loadMore} loading={list.isFetchingNextPage}>
                  Load more
                </Button>
              ) : null}
            </div>
          }
        />
      ) : (
        <Card className="overflow-hidden">
          <ul className="divide-y divide-line" aria-label="Projects">
            {visible.map((project) => (
              <ProjectRow key={project.id} project={project} />
            ))}
          </ul>
          {list.hasNextPage ? (
            <div className="flex justify-center border-t border-line p-4">
              <Button variant="secondary" onClick={loadMore} loading={list.isFetchingNextPage}>
                Load more projects
              </Button>
            </div>
          ) : null}
          {loadMoreFailed ? (
            <div className="border-t border-line p-4">
              <ErrorState error={loadError} action="load more projects" onRetry={loadMore} />
            </div>
          ) : null}
        </Card>
      )}
    </div>
  );
}
