'use client';

import { BookOpen, CheckCircle2, Lock, PlayCircle } from 'lucide-react';
import Link from 'next/link';
import type { MyCourse, MyModule } from '@edutoon/contracts';
import { Badge } from '@/components/ui/badge';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { EmptyState, ErrorState, Skeleton } from '@/components/ui/feedback';
import { PageHeader } from '@/components/ui/page-header';
import { useMyAttempts, useMyCourses } from '@/lib/api/training-queries';
import { formatDuration } from '@/lib/format';
import { bestAttempt } from '@/lib/training';

function ProgressBadge({ module }: { module: MyModule }) {
  const attempts = useMyAttempts(module.id, !module.locked);
  if (module.locked) {
    return (
      <Badge tone="neutral">
        <Lock className="size-3" aria-hidden /> Locked
      </Badge>
    );
  }
  if (attempts.isPending) return <Badge tone="neutral">Checking…</Badge>;
  const summary = bestAttempt(attempts.data ?? []);
  if (summary.passed) {
    return (
      <Badge tone="success" dot>
        Passed · {summary.best.correct_count}/{summary.best.question_count}
      </Badge>
    );
  }
  if (summary.count > 0) {
    return (
      <Badge tone="warning" dot>
        Not passed yet · {summary.count} {summary.count === 1 ? 'attempt' : 'attempts'}
      </Badge>
    );
  }
  return (
    <Badge tone="info" dot>
      Ready to start
    </Badge>
  );
}

function ModuleRow({ module }: { module: MyModule }) {
  const body = (
    <>
      <span
        className={`flex size-10 shrink-0 items-center justify-center rounded-full ${
          module.locked ? 'bg-ink-200/45 text-ink-500' : 'bg-brand-50 text-brand-700'
        }`}
      >
        {module.locked ? (
          <Lock className="size-5" aria-hidden />
        ) : (
          <PlayCircle className="size-5" aria-hidden />
        )}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-semibold text-ink-950">
          {module.position}. {module.title}
        </p>
        <p className="text-[13px] text-ink-500">
          {formatDuration(module.duration_ms)}
          {module.description ? ` · ${module.description}` : ''}
        </p>
        {module.locked && module.lock ? (
          <p className="mt-0.5 text-[13px] text-ink-600">
            Pass &ldquo;{module.lock.blocking_module_title}&rdquo; (module{' '}
            {module.lock.blocking_module_position}) to unlock this one.
          </p>
        ) : null}
      </div>
      <ProgressBadge module={module} />
    </>
  );

  return module.locked ? (
    <li
      aria-label={`${module.title}, locked`}
      className="flex items-center gap-4 py-3 opacity-80 first:pt-0 last:pb-0"
    >
      {body}
    </li>
  ) : (
    <li className="first:pt-0 last:pb-0">
      <Link
        href={`/learn/${module.id}`}
        className="flex items-center gap-4 rounded-md py-3 hover:bg-canvas"
      >
        {body}
      </Link>
    </li>
  );
}

function CourseCard({ course }: { course: MyCourse }) {
  return (
    <Card>
      <CardHeader
        title={course.title}
        description={course.description ?? undefined}
        icon={<BookOpen className="size-4" aria-hidden />}
      />
      <CardBody>
        {course.modules.length === 0 ? (
          <p className="text-sm text-ink-500">No modules have been published yet.</p>
        ) : (
          <ul className="divide-y divide-line" aria-label={`Modules in ${course.title}`}>
            {course.modules.map((module) => (
              <ModuleRow key={module.id} module={module} />
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}

/** An employee's own courses: what is open, what is locked and why, and how far they have got. */
export function MyCoursesView() {
  const courses = useMyCourses();

  return (
    <div className="space-y-6">
      <PageHeader
        title="My learning"
        description="Work through each module in order. A module opens once you pass the one before it."
      />
      {courses.isPending ? (
        <div className="space-y-4" aria-busy="true" aria-label="Loading your courses">
          <Skeleton className="h-40 w-full" />
        </div>
      ) : courses.isError ? (
        <ErrorState
          error={courses.error}
          action="load your courses"
          onRetry={() => void courses.refetch()}
          retrying={courses.isRefetching}
        />
      ) : courses.data.length === 0 ? (
        <EmptyState
          icon={CheckCircle2}
          title="Nothing assigned to you yet"
          description="When your administrator enrols you in a course and publishes it, it appears here."
        />
      ) : (
        courses.data.map((course) => <CourseCard key={course.id} course={course} />)
      )}
    </div>
  );
}
