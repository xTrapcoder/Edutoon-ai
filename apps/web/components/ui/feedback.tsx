import {
  AlertTriangle,
  CheckCircle2,
  Info,
  Loader2,
  RefreshCw,
  XCircle,
  type LucideIcon,
} from 'lucide-react';
import type { HTMLAttributes, ReactNode } from 'react';
import { describeError } from '@/lib/api/errors';
import { cn } from '@/lib/cn';
import { Button } from './button';

type AlertTone = 'info' | 'success' | 'warning' | 'danger';

const alertTones: Record<AlertTone, { box: string; icon: LucideIcon; iconClass: string }> = {
  info: { box: 'border-brand-200 bg-brand-50/60', icon: Info, iconClass: 'text-brand-600' },
  success: {
    box: 'border-success-100 bg-success-50',
    icon: CheckCircle2,
    iconClass: 'text-success-600',
  },
  warning: {
    box: 'border-warning-100 bg-warning-50',
    icon: AlertTriangle,
    iconClass: 'text-warning-600',
  },
  danger: { box: 'border-danger-100 bg-danger-50', icon: XCircle, iconClass: 'text-danger-600' },
};

interface AlertProps extends Omit<HTMLAttributes<HTMLDivElement>, 'title'> {
  tone?: AlertTone;
  title?: ReactNode;
  action?: ReactNode;
}

/** An inline message. Danger and warning ones are announced to screen readers straight away. */
export function Alert({ tone = 'info', title, action, className, children, ...props }: AlertProps) {
  const { box, icon: Icon, iconClass } = alertTones[tone];
  return (
    <div
      role={tone === 'danger' || tone === 'warning' ? 'alert' : 'status'}
      className={cn('flex gap-3 rounded-lg border p-4', box, className)}
      {...props}
    >
      <Icon className={cn('mt-0.5 size-5 shrink-0', iconClass)} aria-hidden />
      <div className="min-w-0 flex-1 space-y-1 text-sm text-ink-700">
        {title ? <p className="font-semibold text-ink-950">{title}</p> : null}
        {children ? <div>{children}</div> : null}
      </div>
      {action ? <div className="shrink-0 self-center">{action}</div> : null}
    </div>
  );
}

interface EmptyStateProps {
  icon: LucideIcon;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ icon: Icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-center rounded-lg border border-dashed border-line-strong bg-surface px-6 py-12 text-center',
        className,
      )}
    >
      <span className="flex size-12 items-center justify-center rounded-full bg-brand-50 text-brand-600">
        <Icon className="size-6" aria-hidden />
      </span>
      <h3 className="mt-4 text-base font-semibold text-ink-950">{title}</h3>
      {description ? <p className="mt-1.5 max-w-md text-sm text-ink-500">{description}</p> : null}
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  );
}

interface ErrorStateProps {
  error: unknown;
  /** What was being attempted, e.g. "load your projects". */
  action?: string;
  onRetry?: () => void;
  retrying?: boolean;
  className?: string;
}

/** A failed load, in words a person can act on, with a way to try again. */
export function ErrorState({
  error,
  action = 'load this',
  onRetry,
  retrying,
  className,
}: ErrorStateProps) {
  const copy = describeError(error, { action });
  return (
    <div
      role="alert"
      className={cn(
        'flex flex-col items-center rounded-lg border border-danger-100 bg-danger-50/60 px-6 py-10 text-center',
        className,
      )}
    >
      <span className="flex size-12 items-center justify-center rounded-full bg-danger-100 text-danger-600">
        <AlertTriangle className="size-6" aria-hidden />
      </span>
      <h3 className="mt-4 text-base font-semibold text-ink-950">{copy.title}</h3>
      <p className="mt-1.5 max-w-md text-sm text-ink-600">{copy.message}</p>
      {onRetry ? (
        <Button
          variant="secondary"
          className="mt-5"
          onClick={onRetry}
          loading={retrying}
          leadingIcon={<RefreshCw className="size-4" aria-hidden />}
        >
          Try again
        </Button>
      ) : null}
    </div>
  );
}

export function Spinner({ className, label }: { className?: string; label?: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2">
      <Loader2 className={cn('size-4 animate-spin text-brand-600', className)} aria-hidden />
      {label ? (
        <span className="text-sm text-ink-600">{label}</span>
      ) : (
        <span className="sr-only">Loading</span>
      )}
    </span>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={cn(
        'animate-shimmer rounded-md bg-[linear-gradient(90deg,var(--color-line)_25%,var(--color-canvas)_50%,var(--color-line)_75%)] bg-[length:200%_100%]',
        className,
      )}
    />
  );
}

interface ProgressProps {
  /** 0 to 100. Leave it out for work whose length is not known. */
  value?: number;
  label: string;
  className?: string;
}

/** A progress bar. Without a `value` it is indeterminate: it shows activity, never a made-up percentage. */
export function Progress({ value, label, className }: ProgressProps) {
  const determinate = value !== undefined;
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={determinate ? 0 : undefined}
      aria-valuemax={determinate ? 100 : undefined}
      aria-valuenow={determinate ? Math.round(value) : undefined}
      className={cn('h-2 w-full overflow-hidden rounded-full bg-brand-100', className)}
    >
      {determinate ? (
        <div
          className="h-full rounded-full bg-brand-600 transition-[width] duration-300"
          style={{ width: `${Math.max(0, Math.min(100, value))}%` }}
        />
      ) : (
        <div className="h-full w-1/3 animate-indeterminate rounded-full bg-brand-600" />
      )}
    </div>
  );
}
