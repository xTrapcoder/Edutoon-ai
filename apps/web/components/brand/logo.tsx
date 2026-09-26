import Link from 'next/link';
import { cn } from '@/lib/cn';

/**
 * The EduToon mark: a mortarboard on an indigo tile. Decorative - the wordmark carries the name.
 *
 * The tile's gradient is CSS, not an SVG `<linearGradient>`: an SVG gradient is referenced by id,
 * and one defined inside a hidden element (the desktop sidebar on a phone) cannot be used by a
 * copy of the mark that is visible.
 */
export function LogoMark({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        'flex size-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-brand-600 to-accent-600',
        className,
      )}
    >
      <svg viewBox="0 0 32 32" className="size-8" focusable="false">
        <path d="M16 7.5 27 13 16 18.5 5 13 16 7.5Z" fill="#fff" />
        <path
          d="M10 16.4v4c0 1.6 2.7 3.1 6 3.1s6-1.5 6-3.1v-4l-6 3-6-3Z"
          fill="#fff"
          fillOpacity=".82"
        />
        <path d="M27 13v6.2" stroke="#fff" strokeWidth="1.6" strokeLinecap="round" />
      </svg>
    </span>
  );
}

export function Logo({ href = '/', className }: { href?: string; className?: string }) {
  return (
    <Link
      href={href}
      className={cn('inline-flex items-center gap-2.5 rounded-md text-ink-950', className)}
      aria-label="EduToon AI home"
    >
      <LogoMark />
      <span className="text-lg font-bold tracking-tight">
        EduToon <span className="text-brand-600">AI</span>
      </span>
    </Link>
  );
}
