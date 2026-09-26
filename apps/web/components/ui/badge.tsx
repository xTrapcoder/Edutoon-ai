import type { HTMLAttributes } from 'react';
import { cn } from '@/lib/cn';
import type { Tone } from '@/lib/workflow';

const tones: Record<Tone, { chip: string; dot: string }> = {
  neutral: { chip: 'bg-ink-200/45 text-ink-700', dot: 'bg-ink-400' },
  info: { chip: 'bg-brand-50 text-brand-700', dot: 'bg-brand-500' },
  brand: { chip: 'bg-accent-50 text-accent-700', dot: 'bg-accent-500' },
  success: { chip: 'bg-success-50 text-success-700', dot: 'bg-success-600' },
  warning: { chip: 'bg-warning-50 text-warning-700', dot: 'bg-warning-600' },
  danger: { chip: 'bg-danger-50 text-danger-700', dot: 'bg-danger-600' },
};

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
  /** A small leading dot; it pulses when `busy`, to show work in progress. */
  dot?: boolean;
  busy?: boolean;
}

export function Badge({
  tone = 'neutral',
  dot = false,
  busy = false,
  className,
  children,
  ...props
}: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap',
        tones[tone].chip,
        className,
      )}
      {...props}
    >
      {dot ? (
        <span
          aria-hidden
          className={cn('size-1.5 rounded-full', tones[tone].dot, busy && 'animate-pulse')}
        />
      ) : null}
      {children}
    </span>
  );
}
