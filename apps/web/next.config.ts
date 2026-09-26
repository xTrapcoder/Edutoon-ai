import path from 'node:path';
import type { NextConfig } from 'next';

/**
 * UI QA mode - a development-only way to render the signed-in screens without a Clerk
 * instance. It swaps `@clerk/nextjs` for the stubs in `test-support/qa` and is meant to be used
 * with `test-support/mock-api`. It is NOT authentication: the stubs sign nobody in, they only
 * pretend a user exists so the pages can be looked at.
 *
 * It refuses to run outside `next dev`, so it can never be part of a build or a deployment.
 */
const uiQaMode = process.env.EDUTOON_UI_QA === '1';
if (uiQaMode && process.env.NODE_ENV === 'production') {
  throw new Error(
    'EDUTOON_UI_QA is a development-only switch that replaces real authentication with a stub. ' +
      'Unset it before running `next build` or `next start`.',
  );
}

// `next dev` always runs from the app's own directory (`pnpm --filter @edutoon/web dev`).
const qaAliases = uiQaMode
  ? {
      '@clerk/nextjs$': path.resolve(process.cwd(), 'test-support/qa/clerk-nextjs.tsx'),
      '@clerk/nextjs/server$': path.resolve(process.cwd(), 'test-support/qa/clerk-server.ts'),
    }
  : {};

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The workspace root, so Next does not guess from a stray lockfile higher up the tree.
  outputFileTracingRoot: path.resolve(process.cwd(), '../..'),
  transpilePackages: ['@edutoon/contracts'],
  webpack(config) {
    if (uiQaMode) {
      config.resolve.alias = { ...config.resolve.alias, ...qaAliases };
    }
    return config;
  },
};

export default nextConfig;
