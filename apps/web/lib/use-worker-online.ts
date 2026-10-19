'use client';

import { useQuery } from '@tanstack/react-query';
import { API_BASE_URL } from '@/lib/api/client';

/**
 * Whether the background worker is running, from the API's public `/health`. `null` while it is
 * unknown (loading, or the API could not be asked) - never guessed as offline. Only asked while
 * something is queued, because a queued job is the one thing a missing worker strands.
 */
export function useWorkerOnline(enabled: boolean): boolean | null {
  const { data } = useQuery({
    queryKey: ['health', 'worker'],
    enabled,
    refetchInterval: enabled ? 10_000 : false,
    retry: false,
    queryFn: async (): Promise<boolean | null> => {
      try {
        const response = await fetch(`${API_BASE_URL}/health`);
        const body = (await response.json()) as { worker?: unknown };
        if (body.worker === 'ok') return true;
        if (body.worker === 'offline') return false;
      } catch {
        // The API itself is unreachable; other parts of the page already report that.
      }
      return null;
    },
  });
  return data ?? null;
}
