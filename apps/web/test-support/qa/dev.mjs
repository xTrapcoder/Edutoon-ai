/**
 * Start the web app in UI QA mode: real pages, a stubbed Clerk, and the mock API.
 * Development only - `next.config.ts` refuses this switch under `next build` / `next start`.
 *
 *   node test-support/mock-api/server.ts   # in one terminal (port 8787)
 *   node test-support/qa/dev.mjs           # in another (port 3000)
 *   node test-support/qa/dev.mjs --real-api # ... against the real API on :8001 instead (QA's own database; the normal app keeps :8000 / :3000)
 */
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';

if (process.env.NODE_ENV === 'production') {
  console.error('UI QA mode is development-only and cannot run with NODE_ENV=production.');
  process.exit(1);
}

const require = createRequire(import.meta.url);
const next = require.resolve('next/dist/bin/next');
const port = process.env.PORT ?? (process.argv.includes('--real-api') ? '3001' : '3000');
// `--real-api` points the stubbed-Clerk app at the real API on :8001 (started by
// `apps/api/scripts/dev_qa_api.py`) instead of the mock API.
const realApi = process.argv.includes('--real-api');

const child = spawn(process.execPath, [next, 'dev', '--port', port], {
  stdio: 'inherit',
  env: {
    ...process.env,
    EDUTOON_UI_QA: '1',
    NEXT_PUBLIC_EDUTOON_UI_QA: '1',
    NEXT_PUBLIC_API_BASE_URL:
      process.env.MOCK_API_URL ?? (realApi ? 'http://localhost:8001' : 'http://localhost:8787'),
    // The stub replaces Clerk, but keep the real package from provisioning anything on its own.
    NEXT_PUBLIC_CLERK_KEYLESS_DISABLED: '1',
  },
});
child.on('exit', (code) => process.exit(code ?? 0));
