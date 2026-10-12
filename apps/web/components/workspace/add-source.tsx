'use client';

import { FileUp, Plus } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { FileDropzone } from '@/components/wizard/file-dropzone';
import { KeywordInput } from '@/components/wizard/keyword-input';
import { fileProblem, mergeKeywords, type FileKind } from '@/components/wizard/wizard-model';
import { Button } from '@/components/ui/button';
import { Alert } from '@/components/ui/feedback';
import { describeError } from '@/lib/api/errors';
import { useAddKeywords, useUploadSource } from '@/lib/api/queries';

/**
 * Upload a PDF or a PowerPoint deck to an existing project - for one that has none yet, or
 * whose upload failed.
 */
export function AddSourceForm({ projectId, kind = 'pdf' }: { projectId: string; kind?: FileKind }) {
  const word = kind === 'pptx' ? 'deck' : 'PDF';
  const [file, setFile] = useState<File | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const upload = useUploadSource(projectId);

  const choose = (next: File | null) => {
    setFile(next);
    setProblem(next ? fileProblem(next, kind) : null);
    upload.reset();
  };

  const submit = () => {
    if (!file || problem) return;
    upload.mutate(file, {
      onSuccess: (source) => {
        setFile(null);
        if (source.status === 'failed') {
          toast.error(`We couldn't read that ${word}`);
        } else {
          toast.success(kind === 'pptx' ? 'Deck added' : 'PDF added');
        }
      },
    });
  };

  const copy = upload.isError
    ? describeError(upload.error, { action: `upload that ${word}` })
    : null;

  return (
    <div className="space-y-4">
      <FileDropzone
        kind={kind}
        file={file}
        onChange={choose}
        error={problem}
        disabled={upload.isPending}
      />
      {copy ? (
        <Alert tone="danger" title={copy.title}>
          {copy.message}
        </Alert>
      ) : null}
      {upload.isPending ? (
        <p role="status" className="text-sm text-ink-600">
          Uploading and reading your {word}. A long document can take a minute.
        </p>
      ) : null}
      <Button
        onClick={submit}
        loading={upload.isPending}
        disabled={!file || problem !== null}
        leadingIcon={<FileUp className="size-4" aria-hidden />}
      >
        Upload {word}
      </Button>
    </div>
  );
}

/** Add keywords to an existing project. Each becomes a topic of the person's own. */
export function AddKeywordsForm({
  projectId,
  disabled,
  disabledReason,
}: {
  projectId: string;
  disabled?: boolean;
  disabledReason?: string;
}) {
  const [keywords, setKeywords] = useState<string[]>([]);
  const [draft, setDraft] = useState('');
  const add = useAddKeywords(projectId);

  const pending = mergeKeywords(keywords, draft).keywords;

  const submit = () => {
    if (pending.length === 0) return;
    add.mutate(pending, {
      onSuccess: () => {
        setKeywords([]);
        setDraft('');
        toast.success(pending.length === 1 ? 'Keyword added' : `${pending.length} keywords added`);
      },
    });
  };

  const copy = add.isError ? describeError(add.error, { action: 'add those keywords' }) : null;

  return (
    <div className="space-y-4">
      <KeywordInput
        keywords={keywords}
        onChange={(next) => {
          setKeywords(next);
          add.reset();
        }}
        onDraftChange={setDraft}
        disabled={disabled || add.isPending}
      />
      {disabled && disabledReason ? (
        <p className="text-[13px] text-ink-500">{disabledReason}</p>
      ) : null}
      {copy ? (
        <Alert tone="danger" title={copy.title}>
          {copy.message}
        </Alert>
      ) : null}
      <Button
        onClick={submit}
        loading={add.isPending}
        disabled={disabled || pending.length === 0}
        leadingIcon={<Plus className="size-4" aria-hidden />}
      >
        Add {pending.length > 1 ? `${pending.length} keywords` : 'keyword'}
      </Button>
    </div>
  );
}
