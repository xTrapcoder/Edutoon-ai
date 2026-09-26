import type { InputHTMLAttributes, ReactNode } from 'react';
import { cn } from '@/lib/cn';

interface RadioCardProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'title'> {
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  badge?: ReactNode;
  className?: string;
}

/**
 * A choice shown as a card. It is a real radio input underneath (visually hidden), so arrow
 * keys, focus and screen readers all behave natively; the card just draws the state.
 */
export function RadioCard({
  title,
  description,
  icon,
  badge,
  className,
  ...input
}: RadioCardProps) {
  return (
    <label
      className={cn(
        'relative flex cursor-pointer gap-3 rounded-lg border border-line-strong bg-surface p-4 shadow-xs transition-[border-color,background-color,box-shadow] hover:border-brand-300 has-checked:border-brand-600 has-checked:bg-brand-50/50 has-checked:shadow-[0_0_0_1px_var(--color-brand-600)] has-focus-visible:outline-2 has-focus-visible:outline-offset-2 has-focus-visible:outline-brand-500 has-disabled:cursor-not-allowed has-disabled:opacity-60',
        className,
      )}
    >
      <input type="radio" className="peer sr-only" {...input} />
      {icon ? (
        <span className="flex size-10 shrink-0 items-center justify-center rounded-md bg-brand-50 text-brand-600">
          {icon}
        </span>
      ) : null}
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-2 text-sm font-semibold text-ink-950">
          {title}
          {badge}
        </span>
        {description ? (
          <span className="mt-0.5 block text-[13px] text-ink-500">{description}</span>
        ) : null}
      </span>
      <span
        aria-hidden
        className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full border-2 border-line-strong bg-surface peer-checked:border-brand-600 peer-checked:[&>span]:scale-100"
      >
        <span className="size-2.5 scale-0 rounded-full bg-brand-600 transition-transform" />
      </span>
    </label>
  );
}
