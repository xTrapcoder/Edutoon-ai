'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { Toaster } from 'sonner';
import { ApiError } from '@/lib/api/errors';

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        // Retry only what could succeed on a second try: a dropped connection or a server fault,
        // never a "not found" or a rejected input. A timeout is retried once, not twice, so a
        // slow API becomes a visible error with a retry button within about a minute.
        retry: (failureCount, error) => {
          if (!(error instanceof ApiError)) return failureCount < 2;
          if (error.kind === 'timeout') return failureCount < 1;
          return error.retryable && failureCount < 2;
        },
        retryDelay: (attempt) => Math.min(1_000 * 2 ** attempt, 8_000),
      },
      mutations: { retry: false },
    },
  });
}

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(makeQueryClient);
  return (
    <QueryClientProvider client={queryClient}>
      {children}
      <Toaster
        position="bottom-right"
        toastOptions={{
          classNames: {
            toast:
              '!rounded-lg !border !border-line !bg-surface !text-ink-800 !shadow-raised !font-sans',
            title: '!font-semibold !text-ink-950',
            description: '!text-ink-500',
          },
        }}
      />
    </QueryClientProvider>
  );
}
