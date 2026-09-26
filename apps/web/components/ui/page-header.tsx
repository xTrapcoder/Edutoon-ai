import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';

interface PageHeaderProps {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  /** Small text above the title, e.g. a breadcrumb or the project's name. */
  eyebrow?: ReactNode;
  className?: string;
}

export function PageHeader({ title, description, actions, eyebrow, className }: PageHeaderProps) {
  return (
    <div className={cn('flex flex-wrap items-end justify-between gap-x-6 gap-y-4', className)}>
      <div className="min-w-0">
        {eyebrow ? <div className="mb-1 text-sm font-medium text-ink-500">{eyebrow}</div> : null}
        <h1 className="text-[28px] leading-9 font-bold tracking-tight text-ink-950 break-anywhere">
          {title}
        </h1>
        {description ? (
          <p className="mt-1.5 max-w-2xl text-[15px] text-ink-500">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-3">{actions}</div> : null}
    </div>
  );
}
