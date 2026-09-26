'use client';

import type { ReactNode } from 'react';
import { ErrorState, Skeleton } from '@/components/ui/feedback';

interface GateQuery {
  isPending: boolean;
  isError: boolean;
  isRefetching: boolean;
  error: unknown;
  refetch: () => unknown;
}

interface QueryGateProps {
  queries: readonly GateQuery[];
  /** What is being loaded, for the error message: "load your topics". */
  action: string;
  label: string;
  children: ReactNode;
}

/**
 * Show a page's content once everything it needs has loaded; until then a skeleton, and if
 * anything failed a plain-language error with a retry - never a spinner with no way out.
 */
export function QueryGate({ queries, action, label, children }: QueryGateProps) {
  const failed = queries.filter((query) => query.isError);
  if (failed.length > 0) {
    return (
      <ErrorState
        error={failed[0]?.error}
        action={action}
        onRetry={() => failed.forEach((query) => void query.refetch())}
        retrying={failed.some((query) => query.isRefetching)}
      />
    );
  }
  if (queries.some((query) => query.isPending)) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label={label}>
        <Skeleton className="h-16 w-full" />
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_1.5fr]">
          <Skeleton className="h-96 w-full" />
          <Skeleton className="h-96 w-full" />
        </div>
      </div>
    );
  }
  return <>{children}</>;
}
