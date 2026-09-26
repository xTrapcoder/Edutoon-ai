import type { HTMLAttributes, ReactNode } from 'react';
import { cn } from '@/lib/cn';

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn('rounded-lg border border-line bg-surface shadow-card', className)}
      {...props}
    />
  );
}

interface CardHeaderProps {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
  as?: 'h2' | 'h3';
  className?: string;
}

export function CardHeader({
  title,
  description,
  action,
  icon,
  as: Heading = 'h2',
  className,
}: CardHeaderProps) {
  return (
    <div className={cn('flex items-start justify-between gap-4 px-6 pt-5', className)}>
      <div className="flex min-w-0 items-start gap-3">
        {icon ? (
          <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md bg-brand-50 text-brand-700">
            {icon}
          </span>
        ) : null}
        <div className="min-w-0">
          <Heading className="text-[15px] leading-6 font-semibold text-ink-950">{title}</Heading>
          {description ? <p className="mt-0.5 text-sm text-ink-500">{description}</p> : null}
        </div>
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

export function CardBody({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('px-6 py-5', className)} {...props} />;
}

export function CardFooter({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'flex flex-wrap items-center justify-end gap-3 border-t border-line px-6 py-4',
        className,
      )}
      {...props}
    />
  );
}
