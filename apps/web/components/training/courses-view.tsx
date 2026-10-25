'use client';

import { GraduationCap, Plus } from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { toast } from 'sonner';
import type { Course } from '@edutoon/contracts';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardBody } from '@/components/ui/card';
import { Alert, EmptyState, ErrorState, Skeleton } from '@/components/ui/feedback';
import { Field, Input, Textarea } from '@/components/ui/field';
import { Modal } from '@/components/ui/modal';
import { PageHeader } from '@/components/ui/page-header';
import { describeError } from '@/lib/api/errors';
import { useCourses, useCreateCourse } from '@/lib/api/training-queries';
import type { Tone } from '@/lib/workflow';

export const COURSE_STATUS: Record<Course['status'], { label: string; tone: Tone }> = {
  draft: { label: 'Draft', tone: 'neutral' },
  published: { label: 'Published', tone: 'success' },
  archived: { label: 'Archived', tone: 'warning' },
};

function NewCourseModal({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const create = useCreateCourse();
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const copy = create.isError ? describeError(create.error, { action: 'create the course' }) : null;

  return (
    <Modal
      open
      onClose={onClose}
      dismissible={!create.isPending}
      title="New course"
      description="A course groups modules in the order employees take them."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={create.isPending}>
            Cancel
          </Button>
          <Button
            loading={create.isPending}
            disabled={title.trim() === ''}
            onClick={() =>
              create.mutate(
                { title: title.trim(), description: description.trim() || null },
                {
                  onSuccess: (course) => {
                    toast.success('Course created');
                    router.push(`/admin/training/${course.id}`);
                  },
                },
              )
            }
          >
            Create course
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Title" required>
          {(control) => (
            <Input
              {...control}
              value={title}
              maxLength={200}
              placeholder="For example: Workplace safety"
              onChange={(event) => setTitle(event.target.value)}
            />
          )}
        </Field>
        <Field label="Description (optional)">
          {(control) => (
            <Textarea
              {...control}
              rows={3}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          )}
        </Field>
        {copy ? (
          <Alert tone="danger" title={copy.title}>
            {copy.message}
          </Alert>
        ) : null}
      </div>
    </Modal>
  );
}

/** The administrator's courses. */
export function CoursesView() {
  const courses = useCourses();
  const [creating, setCreating] = useState(false);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Training"
        description="Courses, their modules, assessments and who is enrolled."
        actions={
          <Button
            onClick={() => setCreating(true)}
            leadingIcon={<Plus className="size-4" aria-hidden />}
          >
            New course
          </Button>
        }
      />
      {courses.isPending ? (
        <Skeleton className="h-40 w-full" />
      ) : courses.isError ? (
        <ErrorState
          error={courses.error}
          action="load the courses"
          onRetry={() => void courses.refetch()}
          retrying={courses.isRefetching}
        />
      ) : courses.data.length === 0 ? (
        <EmptyState
          icon={GraduationCap}
          title="No courses yet"
          description="Create a course, then publish a finished video into it as a module."
        />
      ) : (
        <Card>
          <CardBody className="p-0">
            <ul className="divide-y divide-line" aria-label="Courses">
              {courses.data.map((course) => (
                <li key={course.id}>
                  <Link
                    href={`/admin/training/${course.id}`}
                    className="flex flex-wrap items-center gap-x-4 gap-y-1 px-6 py-4 hover:bg-canvas"
                  >
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-semibold text-ink-950">{course.title}</p>
                      <p className="text-[13px] text-ink-500">
                        {course.published_module_count} of {course.module_count} modules published
                      </p>
                    </div>
                    <Badge tone={COURSE_STATUS[course.status].tone} dot>
                      {COURSE_STATUS[course.status].label}
                    </Badge>
                  </Link>
                </li>
              ))}
            </ul>
          </CardBody>
        </Card>
      )}
      {creating ? <NewCourseModal onClose={() => setCreating(false)} /> : null}
    </div>
  );
}
