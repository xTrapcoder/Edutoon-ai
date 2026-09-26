'use client';

import { AlertTriangle, RefreshCw } from 'lucide-react';
import { useEffect } from 'react';
import { Button, ButtonLink } from '@/components/ui/button';

/**
 * The last line of defence inside the app: if a screen throws while rendering, the person keeps
 * the sidebar and sees a plain message. The error itself is never shown; it goes to the console
 * for whoever is debugging.
 */
export default function AppError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <div
      role="alert"
      className="mx-auto flex max-w-lg flex-col items-center gap-5 py-20 text-center"
    >
      <span className="flex size-14 items-center justify-center rounded-full bg-danger-50 text-danger-600">
        <AlertTriangle className="size-7" aria-hidden />
      </span>
      <div>
        <h1 className="text-2xl font-bold text-ink-950">Something went wrong</h1>
        <p className="mt-2 text-ink-500">
          This page couldn&apos;t be shown. Nothing you have done has been lost. Try again, or go
          back to your projects.
        </p>
      </div>
      <div className="flex flex-wrap justify-center gap-3">
        <Button onClick={reset} leadingIcon={<RefreshCw className="size-4" aria-hidden />}>
          Try again
        </Button>
        <ButtonLink href="/projects" variant="secondary">
          Back to projects
        </ButtonLink>
      </div>
    </div>
  );
}
