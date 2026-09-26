'use client';

import {
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type TextareaHTMLAttributes,
} from 'react';
import { cn } from '@/lib/cn';

const control =
  'w-full rounded-md border border-line-strong bg-surface px-3 text-sm text-ink-900 shadow-xs placeholder:text-ink-400 transition-colors hover:border-ink-300 focus:border-brand-500 focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-brand-500/40 disabled:cursor-not-allowed disabled:bg-canvas disabled:text-ink-500 aria-[invalid=true]:border-danger-600';

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn(control, 'h-10', className)} {...props} />;
}

export function Textarea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn(control, 'min-h-24 py-2.5 leading-6', className)} {...props} />;
}

export interface FieldControlProps {
  id: string;
  'aria-describedby': string | undefined;
  'aria-invalid': boolean | undefined;
  'aria-required': boolean | undefined;
}

interface FieldProps {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  required?: boolean;
  /** Visually hide the label (it is still read out). */
  hideLabel?: boolean;
  className?: string;
  children: (control: FieldControlProps) => ReactNode;
}

/** A label, a hint and an error message, wired to one control with the right `aria-*`. */
export function Field({
  label,
  hint,
  error,
  required,
  hideLabel,
  className,
  children,
}: FieldProps) {
  const uid = useId();
  const id = `${uid}-control`;
  const hintId = hint ? `${uid}-hint` : undefined;
  const errorId = error ? `${uid}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(' ') || undefined;

  return (
    <div className={cn('space-y-1.5', className)}>
      <label
        htmlFor={id}
        className={cn('block text-sm font-semibold text-ink-900', hideLabel && 'sr-only')}
      >
        {label}
        {required ? (
          <span className="ml-0.5 text-danger-600" aria-hidden>
            *
          </span>
        ) : null}
      </label>
      {children({
        id,
        'aria-describedby': describedBy,
        'aria-invalid': error ? true : undefined,
        'aria-required': required || undefined,
      })}
      {hint ? (
        <p id={hintId} className="text-[13px] text-ink-500">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} role="alert" className="text-[13px] font-medium text-danger-700">
          {error}
        </p>
      ) : null}
    </div>
  );
}
