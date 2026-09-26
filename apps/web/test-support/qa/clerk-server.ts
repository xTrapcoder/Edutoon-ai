/**
 * UI QA stand-in for `@clerk/nextjs/server`. Test tooling only - see `next.config.ts` and
 * `clerk-nextjs.tsx`. It lets every request through; it verifies nothing.
 */
import { NextResponse, type NextRequest } from 'next/server';

type Auth = (() => Promise<{ userId: string }>) & { protect: () => Promise<{ userId: string }> };

const auth: Auth = Object.assign(async () => ({ userId: 'ui-qa-user' }), {
  protect: async () => ({ userId: 'ui-qa-user' }),
});

export function clerkMiddleware(
  handler?: (auth: Auth, request: NextRequest) => Promise<unknown> | unknown,
) {
  return async (request: NextRequest) => {
    await handler?.(auth, request);
    return NextResponse.next();
  };
}

/** Patterns such as `/projects(.*)` are already regular expressions once anchored. */
export function createRouteMatcher(patterns: string[]) {
  const expressions = patterns.map((pattern) => new RegExp(`^${pattern}$`));
  return (request: NextRequest) =>
    expressions.some((expression) => expression.test(request.nextUrl.pathname));
}
