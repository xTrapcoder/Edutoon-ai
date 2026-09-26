import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';
import { apiErrorFromFailure, apiErrorFromResponse } from './errors';
import { signatureOf, useIdempotentMutation } from './queries';

const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider
    client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}
  >
    {children}
  </QueryClientProvider>
);

/** A mutation that records the idempotency key of each attempt and fails or succeeds as told. */
function setup(outcomes: (Error | 'ok')[]) {
  const keys: string[] = [];
  const hook = renderHook(
    () =>
      useIdempotentMutation(async (variables: { n: number } | void, key) => {
        keys.push(key);
        const outcome = outcomes.shift() ?? 'ok';
        if (outcome !== 'ok') throw outcome;
        return variables;
      }),
    { wrapper },
  );
  const run = async (variables?: { n: number }) => {
    await act(async () => {
      await hook.result.current.mutateAsync(variables as { n: number }).catch(() => undefined);
    });
  };
  return { keys, run };
}

describe('useIdempotentMutation', () => {
  it('works for a mutation that takes no variables (generate script, delete project)', async () => {
    const { keys, run } = setup(['ok']);
    await run();
    expect(keys).toHaveLength(1);
    expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/);
  });

  it('replays the same key when the outcome was unknown, so the API can deduplicate', async () => {
    const { keys, run } = setup([apiErrorFromFailure(new TypeError('Failed to fetch')), 'ok']);
    await run({ n: 1 });
    await run({ n: 1 });
    expect(keys[1]).toBe(keys[0]);
  });

  it('uses a new key for different input, even after an unknown outcome', async () => {
    const { keys, run } = setup([apiErrorFromFailure(new TypeError('Failed to fetch')), 'ok']);
    await run({ n: 1 });
    await run({ n: 2 });
    expect(keys[1]).not.toBe(keys[0]);
  });

  it('uses a new key after a definite rejection, so a corrected retry is not replayed', async () => {
    const rejected = apiErrorFromResponse(new Response(null, { status: 400 }), null);
    const { keys, run } = setup([rejected, 'ok']);
    await run({ n: 1 });
    await run({ n: 1 });
    expect(keys[1]).not.toBe(keys[0]);
  });

  it('uses a new key after a success, so a second job is never mistaken for a replay', async () => {
    const { keys, run } = setup(['ok', 'ok']);
    await run({ n: 1 });
    await run({ n: 1 });
    expect(keys[1]).not.toBe(keys[0]);
  });
});

describe('signatureOf', () => {
  it('is a string even for no variables, and tells files apart', () => {
    expect(typeof signatureOf(undefined)).toBe('string');
    const a = new File(['a'], 'a.pdf', { lastModified: 1 });
    const b = new File(['bb'], 'a.pdf', { lastModified: 1 });
    expect(signatureOf(a)).not.toBe(signatureOf(b));
  });
});
