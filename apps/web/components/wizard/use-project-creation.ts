'use client';

import { useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import * as api from '@/lib/api/endpoints';
import { newIdempotencyKey } from '@/lib/api/client';
import { ApiError } from '@/lib/api/errors';
import { queryKeys } from '@/lib/api/queries';
import {
  projectSourceType,
  usesFile,
  usesKeywords,
  usesText,
  type WizardValues,
} from './wizard-model';

export type CreationStepKey = 'project' | 'upload' | 'text' | 'keywords';
export type CreationStatus = 'pending' | 'running' | 'done' | 'error';

export interface CreationStep {
  key: CreationStepKey;
  label: string;
  status: CreationStatus;
}

const LABELS: Record<CreationStepKey, string> = {
  project: 'Creating your project',
  upload: 'Uploading and reading your file',
  text: 'Saving and reading your text',
  keywords: 'Saving your keywords',
};

function planFor(values: WizardValues): CreationStepKey[] {
  return [
    'project',
    ...(usesFile(values.mode) ? (['upload'] as const) : []),
    ...(usesText(values.mode) ? (['text'] as const) : []),
    ...(usesKeywords(values.mode) ? (['keywords'] as const) : []),
  ];
}

function signatureOf(values: WizardValues): string {
  return JSON.stringify({
    ...values,
    file: values.file ? [values.file.name, values.file.size, values.file.lastModified] : null,
  });
}

/**
 * Creates a project in up to three requests: the project, then its file (or pasted text), then its keywords.
 * Each request has its own idempotency key, kept while the same form is retried, so a request
 * whose outcome was unknown (a timeout) is replayed rather than done twice. If a later step
 * fails the project already exists: the failure is reported per step and the person can retry
 * from exactly where it stopped, or open the project.
 */
export function useProjectCreation() {
  const queryClient = useQueryClient();
  const [statuses, setStatuses] = useState<Partial<Record<CreationStepKey, CreationStatus>>>({});
  const [plan, setPlan] = useState<CreationStepKey[]>([]);
  const [failure, setFailure] = useState<{ step: CreationStepKey; error: unknown } | null>(null);
  const [running, setRunning] = useState(false);
  const [projectId, setProjectId] = useState<string | null>(null);

  const projectIdRef = useRef<string | null>(null);
  const done = useRef<Set<CreationStepKey>>(new Set());
  const keys = useRef<{
    signature: string;
    project: string;
    upload: string;
    text: string;
    keywords: string;
  } | null>(null);

  const mark = (step: CreationStepKey, status: CreationStatus) =>
    setStatuses((current) => ({ ...current, [step]: status }));

  /** Run whatever has not finished yet. Resolves with the project's id once everything is done. */
  async function run(values: WizardValues): Promise<string | null> {
    const signature = signatureOf(values);
    if (!keys.current || (keys.current.signature !== signature && projectIdRef.current === null)) {
      keys.current = {
        signature,
        project: newIdempotencyKey(),
        upload: newIdempotencyKey(),
        text: newIdempotencyKey(),
        keywords: newIdempotencyKey(),
      };
    }
    const attemptKeys = keys.current;
    const steps = planFor(values);

    setPlan(steps);
    setFailure(null);
    setRunning(true);

    for (const step of steps) {
      if (done.current.has(step)) continue;
      mark(step, 'running');
      try {
        if (step === 'project') {
          const project = await api.createProject(
            {
              title: values.title.trim(),
              source_type: projectSourceType(values.mode),
              language: values.language,
              orientation: values.orientation,
              target_duration_ms: values.durationMs,
            },
            attemptKeys.project,
          );
          projectIdRef.current = project.id;
          setProjectId(project.id);
        } else if (step === 'upload' && projectIdRef.current && values.file) {
          await api.uploadSource(projectIdRef.current, values.file, attemptKeys.upload);
        } else if (step === 'text' && projectIdRef.current) {
          await api.pasteTextSource(
            projectIdRef.current,
            { title: values.title.trim(), text: values.text },
            attemptKeys.text,
          );
        } else if (step === 'keywords' && projectIdRef.current) {
          await api.addKeywords(projectIdRef.current, values.keywords, attemptKeys.keywords);
        }
        done.current.add(step);
        mark(step, 'done');
      } catch (error) {
        // A definite rejection means the same key must not be replayed with new input.
        if (!(error instanceof ApiError && error.outcomeUnknown)) {
          keys.current = { ...attemptKeys, [step]: newIdempotencyKey() };
        }
        mark(step, 'error');
        setFailure({ step, error });
        setRunning(false);
        return null;
      }
    }

    setRunning(false);
    void queryClient.invalidateQueries({ queryKey: queryKeys.projectList });
    return projectIdRef.current;
  }

  const steps: CreationStep[] = plan.map((key) => ({
    key,
    label: LABELS[key],
    status: statuses[key] ?? 'pending',
  }));

  return { steps, run, running, failure, projectId, started: plan.length > 0 };
}
