'use client';

import { Trash2 } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { toast } from 'sonner';
import type { Project } from '@edutoon/contracts';
import { Button } from '@/components/ui/button';
import { Card, CardBody } from '@/components/ui/card';
import { Alert } from '@/components/ui/feedback';
import { Modal } from '@/components/ui/modal';
import { describeError } from '@/lib/api/errors';
import { useDeleteProject } from '@/lib/api/queries';

export function DeleteProjectCard({ project }: { project: Project }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const remove = useDeleteProject(project.id);

  const confirm = () => {
    remove.mutate(undefined, {
      onSuccess: () => {
        toast.success('Project deleted');
        router.replace('/projects');
      },
    });
  };

  const close = () => {
    if (remove.isPending) return;
    remove.reset();
    setOpen(false);
  };

  const copy = remove.isError
    ? describeError(remove.error, { action: 'delete this project' })
    : null;

  return (
    <>
      <Card>
        <CardBody className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h2 className="text-[15px] font-semibold text-ink-950">Delete this project</h2>
            <p className="mt-0.5 text-sm text-ink-500">
              Removes the project, its uploaded PDF, topics, scripts and activity. This cannot be
              undone.
            </p>
          </div>
          <Button
            variant="secondary"
            onClick={() => setOpen(true)}
            leadingIcon={<Trash2 className="size-4" aria-hidden />}
          >
            Delete project
          </Button>
        </CardBody>
      </Card>

      <Modal
        open={open}
        onClose={close}
        dismissible={!remove.isPending}
        size="sm"
        title="Delete this project?"
        description={`"${project.title}" and everything in it will be removed for good.`}
        footer={
          <>
            <Button variant="secondary" onClick={close} disabled={remove.isPending}>
              Keep project
            </Button>
            <Button variant="danger" onClick={confirm} loading={remove.isPending}>
              Delete project
            </Button>
          </>
        }
      >
        {copy ? (
          <Alert tone="danger" title={copy.title}>
            {copy.message}
          </Alert>
        ) : (
          <p className="text-sm text-ink-600">
            Its uploaded PDF, topics, scripts and activity are deleted with it.
          </p>
        )}
      </Modal>
    </>
  );
}
