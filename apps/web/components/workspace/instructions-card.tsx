'use client';

import { MessageSquareText } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import type { InstructionNote } from '@edutoon/contracts';
import { MAX_INSTRUCTIONS_CHARS, instructionsProblem } from '@/components/wizard/wizard-model';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Alert } from '@/components/ui/feedback';
import { Field, Textarea } from '@/components/ui/field';
import { describeError } from '@/lib/api/errors';
import { useUpdateProject } from '@/lib/api/queries';
import type { Tone } from '@/lib/workflow';
import { useWorkspaceContext } from './workspace-context';

const KIND: Record<InstructionNote['kind'], { label: string; tone: Tone }> = {
  applied: { label: 'Applied', tone: 'success' },
  guidance: { label: 'Guidance only', tone: 'info' },
  unsupported: { label: "Can't be done", tone: 'warning' },
};

/**
 * The person's instructions for the lesson, and what became of each part: done in code, passed
 * to the writers as guidance (best effort, not a guarantee) or something the product cannot do.
 * Never a silent drop. Not shown for a PowerPoint deck, which no model writes.
 */
export function InstructionsCard() {
  const { project } = useWorkspaceContext();
  const update = useUpdateProject(project.id);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(project.instructions ?? '');

  if (project.source_type === 'pptx') return null;

  const problem = instructionsProblem(draft);
  const unchanged = draft.trim() === (project.instructions ?? '');
  const copy = update.isError
    ? describeError(update.error, { action: 'save your instructions' })
    : null;

  const save = () => {
    update.mutate(
      { instructions: draft.trim() === '' ? null : draft.trim() },
      {
        onSuccess: () => {
          toast.success('Instructions saved');
          setEditing(false);
        },
      },
    );
  };

  return (
    <Card id="instructions" className="scroll-mt-24">
      <CardHeader
        title="Instructions"
        description="How you want the lesson, in your own words."
        icon={<MessageSquareText className="size-4" aria-hidden />}
        action={
          editing ? null : (
            <Button
              size="sm"
              variant="secondary"
              onClick={() => {
                setDraft(project.instructions ?? '');
                setEditing(true);
              }}
            >
              {project.instructions ? 'Edit' : 'Add'}
            </Button>
          )
        }
      />
      <CardBody className="space-y-4">
        {editing ? (
          <>
            <Field
              label="Instructions"
              error={problem}
              hint={`They apply the next time topics, claims, a script or scenes are generated. ${draft.trim().length.toLocaleString('en-GB')} of ${MAX_INSTRUCTIONS_CHARS.toLocaleString('en-GB')} characters. They can never add a fact your source does not contain.`}
            >
              {(control) => (
                <Textarea
                  {...control}
                  rows={4}
                  value={draft}
                  disabled={update.isPending}
                  placeholder="For example: Make it suitable for a beginner and explain every term."
                  onChange={(event) => {
                    setDraft(event.target.value);
                    update.reset();
                  }}
                />
              )}
            </Field>
            {copy ? (
              <Alert tone="danger" title={copy.title}>
                {copy.message}
              </Alert>
            ) : null}
            <div className="flex gap-3">
              <Button
                onClick={save}
                loading={update.isPending}
                disabled={problem !== null || unchanged}
              >
                Save instructions
              </Button>
              <Button
                variant="secondary"
                disabled={update.isPending}
                onClick={() => {
                  setEditing(false);
                  update.reset();
                }}
              >
                Cancel
              </Button>
            </div>
          </>
        ) : project.instructions ? (
          <>
            <p className="rounded-md bg-surface-sunken px-3 py-2 text-sm whitespace-pre-wrap text-ink-800">
              {project.instructions}
            </p>
            <ul className="space-y-2" aria-label="What happened to each part of your instructions">
              {project.instructions_notes.map((note) => (
                <li key={`${note.kind}-${note.text}`} className="flex items-start gap-2.5">
                  <Badge tone={KIND[note.kind].tone} className="mt-0.5 shrink-0">
                    {KIND[note.kind].label}
                  </Badge>
                  <span className="text-[13px] text-ink-700">{note.text}</span>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="text-sm text-ink-500">
            No instructions. Add some to steer which topics are covered, who the lesson is for and
            how it explains things.
          </p>
        )}
      </CardBody>
    </Card>
  );
}
