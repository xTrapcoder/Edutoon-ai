'use client';

import {
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  ClipboardList,
  Eye,
  EyeOff,
  Trash2,
  UserPlus,
  Video,
} from 'lucide-react';
import Link from 'next/link';
import { useEffect, useState, type SelectHTMLAttributes } from 'react';
import { toast } from 'sonner';
import { useQuery } from '@tanstack/react-query';
import type { Module, Orientation, Project, RenderQuality } from '@edutoon/contracts';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Alert, ErrorState, Skeleton } from '@/components/ui/feedback';
import { Field, Input } from '@/components/ui/field';
import { Modal } from '@/components/ui/modal';
import { PageHeader } from '@/components/ui/page-header';
import { cn } from '@/lib/cn';
import { listProjects, listScripts } from '@/lib/api/endpoints';
import { describeError } from '@/lib/api/errors';
import { fetchModulePreviewUrl } from '@/lib/api/training';
import {
  useArchiveCourse,
  useCourse,
  useDeleteModule,
  useEnrolUser,
  useEnrolments,
  useInviteUser,
  useModules,
  usePublishCourse,
  usePublishModule,
  useReorderModules,
  useUnpublishModule,
  useUsers,
  useWithdrawUser,
} from '@/lib/api/training-queries';
import { formatBytes, formatDateTime, formatDuration } from '@/lib/format';
import { AssessmentEditor } from './assessment-editor';
import { COURSE_STATUS } from './courses-view';

function Select({ className, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn(
        'h-10 w-full rounded-md border border-line-strong bg-surface px-3 text-sm text-ink-900 focus:border-brand-500',
        className,
      )}
      {...props}
    />
  );
}

function MutationError({ error, action }: { error: unknown; action: string }) {
  if (!error) return null;
  const copy = describeError(error, { action });
  return (
    <Alert tone="danger" title={copy.title}>
      {copy.message}
    </Alert>
  );
}

// --- preview ----------------------------------------------------------------------------

function PreviewModal({ module, onClose }: { module: Module; onClose: () => void }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    fetchModulePreviewUrl(module.id)
      .then((fetched) => {
        if (cancelled) URL.revokeObjectURL(fetched);
        else {
          objectUrl = fetched;
          setUrl(fetched);
        }
      })
      .catch(() => !cancelled && setFailed(true));
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [module.id]);
  return (
    <Modal open onClose={onClose} size="lg" title={`Preview: ${module.title}`}>
      {failed ? (
        <Alert tone="danger" title="The preview could not be loaded" />
      ) : url ? (
        <video controls src={url} className="aspect-video w-full rounded-lg bg-ink-950" />
      ) : (
        <Skeleton className="aspect-video w-full" />
      )}
    </Modal>
  );
}

// --- modules ----------------------------------------------------------------------------

function ModulesCard({ courseId }: { courseId: string }) {
  const modules = useModules(courseId);
  const reorder = useReorderModules(courseId);
  const unpublish = useUnpublishModule(courseId);
  const remove = useDeleteModule(courseId);
  const [previewing, setPreviewing] = useState<Module | null>(null);
  const [editing, setEditing] = useState<Module | null>(null);
  const [deleting, setDeleting] = useState<Module | null>(null);

  const list = modules.data ?? [];
  const move = (index: number, by: -1 | 1) => {
    const ids = list.map((module) => module.id);
    const [moved] = ids.splice(index, 1);
    ids.splice(index + by, 0, moved!);
    reorder.mutate(ids);
  };

  return (
    <Card>
      <CardHeader
        title="Modules"
        description="Employees take them in this order; each opens once the one before is passed."
        icon={<Video className="size-4" aria-hidden />}
      />
      <CardBody className="space-y-3">
        {modules.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : modules.isError ? (
          <ErrorState
            error={modules.error}
            action="load the modules"
            onRetry={() => void modules.refetch()}
          />
        ) : list.length === 0 ? (
          <p className="text-sm text-ink-500">No modules yet. Publish a finished video below.</p>
        ) : (
          <ol className="divide-y divide-line" aria-label="Modules">
            {list.map((module, index) => (
              <li key={module.id} className="flex flex-wrap items-center gap-3 py-3">
                <span className="w-6 text-sm font-semibold text-ink-500">{module.position}</span>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-semibold text-ink-950">{module.title}</p>
                  <p className="text-[13px] text-ink-500">
                    {formatDuration(module.video_duration_ms)} · {module.video_width}x
                    {module.video_height} · {formatBytes(module.video_byte_size)} · published{' '}
                    {formatDateTime(module.published_at)}
                  </p>
                </div>
                <Badge tone={module.status === 'published' ? 'success' : 'neutral'} dot>
                  {module.status === 'published' ? 'Published' : 'Unpublished'}
                </Badge>
                <div className="flex flex-wrap gap-1.5">
                  <Button size="sm" variant="secondary" onClick={() => setPreviewing(module)}>
                    Preview
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => setEditing(module)}
                    leadingIcon={<ClipboardList className="size-4" aria-hidden />}
                  >
                    Assessment
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    aria-label={`Move ${module.title} up`}
                    disabled={index === 0 || reorder.isPending}
                    onClick={() => move(index, -1)}
                    leadingIcon={<ArrowUp className="size-4" aria-hidden />}
                  >
                    {' '}
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    aria-label={`Move ${module.title} down`}
                    disabled={index === list.length - 1 || reorder.isPending}
                    onClick={() => move(index, 1)}
                    leadingIcon={<ArrowDown className="size-4" aria-hidden />}
                  >
                    {' '}
                  </Button>
                  {module.status === 'published' ? (
                    <Button
                      size="sm"
                      variant="secondary"
                      loading={unpublish.isPending && unpublish.variables === module.id}
                      onClick={() =>
                        unpublish.mutate(module.id, {
                          onSuccess: () => toast.success('Module unpublished'),
                        })
                      }
                      leadingIcon={<EyeOff className="size-4" aria-hidden />}
                    >
                      Unpublish
                    </Button>
                  ) : null}
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => setDeleting(module)}
                    leadingIcon={<Trash2 className="size-4" aria-hidden />}
                  >
                    Delete
                  </Button>
                </div>
              </li>
            ))}
          </ol>
        )}
        <MutationError error={reorder.error ?? unpublish.error} action="change the modules" />
      </CardBody>
      {previewing ? <PreviewModal module={previewing} onClose={() => setPreviewing(null)} /> : null}
      {editing ? <AssessmentEditor module={editing} onClose={() => setEditing(null)} /> : null}
      <Modal
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        size="sm"
        title="Delete this module?"
        description={
          deleting
            ? `"${deleting.title}" and its copy of the video are removed. A module employees have attempted cannot be deleted - unpublish it instead.`
            : undefined
        }
        footer={
          <>
            <Button variant="secondary" onClick={() => setDeleting(null)}>
              Keep it
            </Button>
            <Button
              loading={remove.isPending}
              onClick={() =>
                deleting &&
                remove.mutate(deleting.id, {
                  onSuccess: () => {
                    toast.success('Module deleted');
                    setDeleting(null);
                  },
                })
              }
            >
              Delete module
            </Button>
          </>
        }
      >
        <MutationError error={remove.error} action="delete the module" />
      </Modal>
    </Card>
  );
}

// --- publish a finished video as a module -----------------------------------------------

function useReadyProjects() {
  return useQuery({
    queryKey: ['training', 'ready-projects'],
    queryFn: async () => (await listProjects({ limit: 100 })).items,
  });
}

function AddModuleCard({ courseId }: { courseId: string }) {
  const projects = useReadyProjects();
  const publish = usePublishModule(courseId);
  const [projectId, setProjectId] = useState('');
  const [orientation, setOrientation] = useState<Orientation>('horizontal');
  const [quality, setQuality] = useState<RenderQuality>('high');
  const [title, setTitle] = useState('');

  const ready = (projects.data ?? []).filter((project: Project) => project.status === 'ready');
  const scripts = useQuery({
    queryKey: ['training', 'project-scripts', projectId],
    queryFn: () => listScripts(projectId),
    enabled: projectId !== '',
  });
  const approved = (scripts.data ?? []).find((script) => script.status === 'approved');

  const choose = (id: string) => {
    setProjectId(id);
    const project = ready.find((candidate) => candidate.id === id);
    if (project) {
      setOrientation(project.orientation);
      setTitle(project.title);
    }
  };

  return (
    <Card>
      <CardHeader
        title="Publish a finished video as a module"
        description="Pick a project whose video has been rendered. The module gets its own copy of that video."
        icon={<Video className="size-4" aria-hidden />}
      />
      <CardBody className="space-y-4">
        <Field label="Project">
          {(control) => (
            <Select {...control} value={projectId} onChange={(event) => choose(event.target.value)}>
              <option value="">Choose a project…</option>
              {ready.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.title}
                </option>
              ))}
            </Select>
          )}
        </Field>
        {projectId && scripts.isSuccess && !approved ? (
          <Alert tone="warning" title="This project has no approved script">
            A module is made from a rendered video, which needs an approved script.
          </Alert>
        ) : null}
        <div className="grid gap-4 sm:grid-cols-3">
          <Field label="Orientation">
            {(control) => (
              <Select
                {...control}
                value={orientation}
                onChange={(event) => setOrientation(event.target.value as Orientation)}
              >
                <option value="horizontal">Horizontal</option>
                <option value="vertical">Vertical</option>
              </Select>
            )}
          </Field>
          <Field label="Quality">
            {(control) => (
              <Select
                {...control}
                value={quality}
                onChange={(event) => setQuality(event.target.value as RenderQuality)}
              >
                <option value="high">High (720p)</option>
                <option value="low">Low (review)</option>
              </Select>
            )}
          </Field>
          <Field label="Module title">
            {(control) => (
              <Input
                {...control}
                value={title}
                onChange={(event) => setTitle(event.target.value)}
              />
            )}
          </Field>
        </div>
        <MutationError error={publish.error} action="publish this video" />
        <Button
          loading={publish.isPending}
          disabled={!projectId || !approved}
          onClick={() =>
            approved &&
            publish.mutate(
              {
                project_id: projectId,
                script_id: approved.id,
                orientation,
                quality,
                title: title.trim() || undefined,
              },
              { onSuccess: () => toast.success('Module published') },
            )
          }
        >
          Publish as a module
        </Button>
      </CardBody>
    </Card>
  );
}

// --- people -----------------------------------------------------------------------------

function EnrolmentsCard({ courseId }: { courseId: string }) {
  const enrolments = useEnrolments(courseId);
  const users = useUsers();
  const enrol = useEnrolUser(courseId);
  const withdraw = useWithdrawUser(courseId);
  const invite = useInviteUser();
  const [userId, setUserId] = useState('');
  const [email, setEmail] = useState('');

  const rows = enrolments.data ?? [];
  const activeIds = new Set(
    rows.filter((row) => row.status === 'active').map((row) => row.user_id),
  );
  const candidates = (users.data ?? []).filter(
    (user) => user.role === 'employee' && user.status === 'active' && !activeIds.has(user.id),
  );

  return (
    <Card>
      <CardHeader
        title="Who is enrolled"
        description="Only an enrolled employee sees the course, and only once it is published."
        icon={<UserPlus className="size-4" aria-hidden />}
      />
      <CardBody className="space-y-5">
        {enrolments.isPending ? (
          <Skeleton className="h-16 w-full" />
        ) : rows.length === 0 ? (
          <p className="text-sm text-ink-500">Nobody is enrolled yet.</p>
        ) : (
          <ul className="divide-y divide-line" aria-label="Enrolments">
            {rows.map((row) => (
              <li key={row.id} className="flex flex-wrap items-center gap-3 py-2.5">
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-semibold text-ink-950">
                    {row.user_display_name ?? row.user_email}
                  </p>
                  {row.user_display_name ? (
                    <p className="text-[13px] text-ink-500">{row.user_email}</p>
                  ) : null}
                </div>
                <Badge tone={row.status === 'active' ? 'success' : 'neutral'} dot>
                  {row.status === 'active' ? 'Enrolled' : 'Withdrawn'}
                </Badge>
                {row.status === 'active' ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    loading={withdraw.isPending && withdraw.variables === row.user_id}
                    onClick={() => withdraw.mutate(row.user_id)}
                  >
                    Withdraw
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        )}

        <div className="flex flex-wrap items-end gap-3">
          <Field label="Enrol an employee" className="min-w-64 flex-1">
            {(control) => (
              <Select
                {...control}
                value={userId}
                onChange={(event) => setUserId(event.target.value)}
              >
                <option value="">Choose an employee…</option>
                {candidates.map((user) => (
                  <option key={user.id} value={user.id}>
                    {user.display_name ? `${user.display_name} (${user.email})` : user.email}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Button
            loading={enrol.isPending}
            disabled={!userId}
            onClick={() =>
              enrol.mutate(userId, {
                onSuccess: () => {
                  toast.success('Enrolled');
                  setUserId('');
                },
              })
            }
          >
            Enrol
          </Button>
        </div>

        <div className="flex flex-wrap items-end gap-3 border-t border-line pt-4">
          <Field
            label="Invite someone new"
            className="min-w-64 flex-1"
            hint="They can sign up with this email address; then enrol them above."
          >
            {(control) => (
              <Input
                {...control}
                type="email"
                value={email}
                placeholder="name@company.com"
                onChange={(event) => setEmail(event.target.value)}
              />
            )}
          </Field>
          <Button
            variant="secondary"
            loading={invite.isPending}
            disabled={!email.includes('@')}
            onClick={() =>
              invite.mutate(
                { email: email.trim(), role: 'employee' },
                {
                  onSuccess: () => {
                    toast.success('Invitation recorded');
                    setEmail('');
                  },
                },
              )
            }
          >
            Invite
          </Button>
        </div>
        <MutationError
          error={enrol.error ?? withdraw.error ?? invite.error}
          action="change enrolments"
        />
      </CardBody>
    </Card>
  );
}

// --- the course -------------------------------------------------------------------------

export function CourseDetail({ courseId }: { courseId: string }) {
  const course = useCourse(courseId);
  const publish = usePublishCourse(courseId);
  const archive = useArchiveCourse(courseId);

  if (course.isPending) return <Skeleton className="h-64 w-full" />;
  if (course.isError) {
    return (
      <ErrorState
        error={course.error}
        action="load this course"
        onRetry={() => void course.refetch()}
        retrying={course.isRefetching}
      />
    );
  }
  const data = course.data;
  const status = COURSE_STATUS[data.status];

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={
          <Link href="/admin/training" className="inline-flex items-center gap-1 hover:underline">
            <ArrowLeft className="size-4" aria-hidden /> All courses
          </Link>
        }
        title={data.title}
        description={data.description ?? undefined}
        actions={
          <>
            <Badge tone={status.tone} dot>
              {status.label}
            </Badge>
            {data.status === 'draft' ? (
              <Button
                loading={publish.isPending}
                disabled={data.published_module_count === 0}
                title={data.published_module_count === 0 ? 'Publish a module first' : undefined}
                onClick={() =>
                  publish.mutate(undefined, { onSuccess: () => toast.success('Course published') })
                }
                leadingIcon={<Eye className="size-4" aria-hidden />}
              >
                Publish course
              </Button>
            ) : null}
            {data.status !== 'archived' ? (
              <Button
                variant="secondary"
                loading={archive.isPending}
                onClick={() =>
                  archive.mutate(undefined, { onSuccess: () => toast.success('Course archived') })
                }
              >
                Archive
              </Button>
            ) : null}
          </>
        }
      />
      <MutationError error={publish.error ?? archive.error} action="change the course" />
      <ModulesCard courseId={courseId} />
      <AddModuleCard courseId={courseId} />
      <EnrolmentsCard courseId={courseId} />
    </div>
  );
}
