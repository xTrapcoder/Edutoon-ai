'use client';

import { FileText, UploadCloud, X } from 'lucide-react';
import { useId, useRef, useState, type DragEvent } from 'react';
import { cn } from '@/lib/cn';
import { formatBytes } from '@/lib/format';
import { MAX_UPLOAD_MB } from './wizard-model';

interface FileDropzoneProps {
  file: File | null;
  onChange: (file: File | null) => void;
  error?: string | null;
  disabled?: boolean;
}

/** Drag a PDF in or browse for one. The native file input stays in the tab order, so it works by keyboard. */
export function FileDropzone({ file, onChange, error, disabled }: FileDropzoneProps) {
  const inputId = useId();
  const errorId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const take = (files: FileList | null) => {
    const first = files?.[0];
    if (first) onChange(first);
  };

  const onDrop = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    setDragging(false);
    if (!disabled) take(event.dataTransfer.files);
  };

  return (
    <div className="space-y-3">
      <label
        htmlFor={inputId}
        onDragOver={(event) => {
          event.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={cn(
          'flex cursor-pointer flex-col items-center rounded-lg border-2 border-dashed px-6 py-10 text-center transition-colors has-focus-visible:outline-2 has-focus-visible:outline-offset-2 has-focus-visible:outline-brand-500',
          dragging
            ? 'border-brand-500 bg-brand-50'
            : 'border-line-strong bg-surface-sunken hover:border-brand-300',
          error && 'border-danger-600',
          disabled && 'cursor-not-allowed opacity-60',
        )}
      >
        <span className="flex size-12 items-center justify-center rounded-full bg-brand-50 text-brand-600">
          <UploadCloud className="size-6" aria-hidden />
        </span>
        <span className="mt-3 text-sm font-semibold text-ink-950">
          Drag and drop your PDF here, or <span className="text-brand-700 underline">browse</span>
        </span>
        <span className="mt-1 text-[13px] text-ink-500">PDF files up to {MAX_UPLOAD_MB} MB</span>
        <input
          ref={inputRef}
          id={inputId}
          type="file"
          accept="application/pdf,.pdf"
          className="sr-only"
          disabled={disabled}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
          onChange={(event) => {
            take(event.target.files);
            // Allow choosing the same file again after removing it.
            event.target.value = '';
          }}
        />
      </label>

      {error ? (
        <p id={errorId} role="alert" className="text-[13px] font-medium text-danger-700">
          {error}
        </p>
      ) : null}

      {file ? (
        <div className="flex items-center gap-3 rounded-lg border border-line bg-surface p-3 shadow-xs">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-md bg-danger-50 text-danger-600">
            <FileText className="size-5" aria-hidden />
          </span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold text-ink-950">{file.name}</p>
            <p className="text-xs text-ink-500">{formatBytes(file.size)}</p>
          </div>
          <button
            type="button"
            onClick={() => onChange(null)}
            disabled={disabled}
            aria-label={`Remove ${file.name}`}
            className="rounded-md p-2 text-ink-500 hover:bg-canvas hover:text-ink-900 disabled:opacity-50"
          >
            <X className="size-4" aria-hidden />
          </button>
        </div>
      ) : null}
    </div>
  );
}
