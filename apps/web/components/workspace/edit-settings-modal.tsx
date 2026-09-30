'use client';

import { useState } from 'react';
import { toast } from 'sonner';
import type { Project, ProjectUpdateRequest } from '@edutoon/contracts';
import {
  GenerationSettingsFields,
  type GenerationSettingsValue,
} from '@/components/settings/generation-settings-fields';
import { Button } from '@/components/ui/button';
import { Alert } from '@/components/ui/feedback';
import { Modal } from '@/components/ui/modal';
import { describeError } from '@/lib/api/errors';
import { useUpdateProject } from '@/lib/api/queries';
import { isValidDurationMs } from '@/lib/generation-settings';

function valuesFrom(project: Project): GenerationSettingsValue {
  return {
    orientation: project.orientation,
    durationMs: project.target_duration_ms,
    language: project.language,
  };
}

export function EditSettingsModal({
  project,
  open,
  onClose,
}: {
  project: Project;
  open: boolean;
  onClose: () => void;
}) {
  const [value, setValue] = useState<GenerationSettingsValue>(() => valuesFrom(project));
  const update = useUpdateProject(project.id);

  // Only what changed is sent (the API's PATCH leaves omitted fields alone).
  const patch: ProjectUpdateRequest = {};
  if (value.orientation !== project.orientation) patch.orientation = value.orientation;
  if (value.language !== project.language) patch.language = value.language;
  if (value.durationMs !== project.target_duration_ms) {
    patch.target_duration_ms = value.durationMs;
  }
  const changed = Object.keys(patch).length > 0;
  const valid = isValidDurationMs(value.durationMs);

  const close = () => {
    if (update.isPending) return;
    setValue(valuesFrom(project));
    update.reset();
    onClose();
  };

  const save = () => {
    update.mutate(patch, {
      onSuccess: () => {
        toast.success('Settings saved');
        onClose();
      },
    });
  };

  const copy = update.isError
    ? describeError(update.error, { action: 'save your settings' })
    : null;

  return (
    <Modal
      open={open}
      onClose={close}
      dismissible={!update.isPending}
      title="Generation settings"
      description="These apply the next time topics or a script are generated. Anything already generated is left as it is."
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={update.isPending}>
            Cancel
          </Button>
          <Button onClick={save} loading={update.isPending} disabled={!changed || !valid}>
            Save settings
          </Button>
        </>
      }
    >
      <div className="space-y-5">
        <GenerationSettingsFields
          value={value}
          onChange={setValue}
          disabled={update.isPending}
          framed={false}
        />
        {copy ? (
          <Alert tone="danger" title={copy.title}>
            {copy.message}
          </Alert>
        ) : null}
      </div>
    </Modal>
  );
}
