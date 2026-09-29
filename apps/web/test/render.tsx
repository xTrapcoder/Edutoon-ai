import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import type { ReactElement, ReactNode } from 'react';
import { WorkspaceShell } from '@/components/workspace/workspace-shell';
import {
  claim,
  job,
  project,
  script,
  scriptDetail,
  segmentStoryboard,
  source,
  storyboardStatus,
  synthesisStatus,
  topic,
  PROJECT_ID,
} from './fixtures';
import { navigation } from './navigation';
import { API, server } from './server';

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 }, mutations: { retry: false } },
  });
}

export function renderWithClient(ui: ReactElement) {
  const client = makeClient();
  const wrap = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, ...render(ui, { wrapper: wrap }) };
}

/** Everything a project reports. Pass only what a test cares about; the rest is empty. */
export interface ProjectApi {
  project?: ReturnType<typeof project>;
  sources?: ReturnType<typeof source>[];
  topics?: ReturnType<typeof topic>[];
  jobs?: ReturnType<typeof job>[];
  scripts?: ReturnType<typeof script>[];
  scriptDetail?: ReturnType<typeof scriptDetail>;
  synthesisStatus?: ReturnType<typeof synthesisStatus>;
  storyboardStatus?: ReturnType<typeof storyboardStatus>;
  claims?: ReturnType<typeof claim>[];
}

const page = <T,>(items: T[]) => ({ items, next_cursor: null });

/** Answer a project's read routes from the given data. Returns nothing; use `server.use` for writes. */
export function serveProject(data: ProjectApi = {}) {
  const base = `${API}/v1/projects/${PROJECT_ID}`;
  server.use(
    http.get(base, () => HttpResponse.json(data.project ?? project())),
    http.get(`${base}/sources`, () => HttpResponse.json(page(data.sources ?? []))),
    http.get(`${base}/topics`, () => HttpResponse.json({ items: data.topics ?? [] })),
    http.get(`${base}/jobs`, () => HttpResponse.json(page(data.jobs ?? []))),
    http.get(`${base}/scripts`, () => HttpResponse.json(page(data.scripts ?? []))),
    http.get(`${base}/scripts/:scriptId`, () =>
      data.scriptDetail
        ? HttpResponse.json(data.scriptDetail)
        : new HttpResponse(null, { status: 404 }),
    ),
    http.get(`${base}/scripts/:scriptId/synthesis`, () => {
      if (data.synthesisStatus) return HttpResponse.json(data.synthesisStatus);
      if (!data.scriptDetail) return new HttpResponse(null, { status: 404 });
      return HttpResponse.json(
        synthesisStatus({
          script: data.scriptDetail,
          segments: data.scriptDetail.segments.map((segment) => ({
            id: segment.id,
            segment_index: segment.segment_index,
            estimated_duration_ms: segment.estimated_duration_ms,
            audio: null,
          })),
        }),
      );
    }),
    http.get(`${base}/scripts/:scriptId/storyboard`, () => {
      if (data.storyboardStatus) return HttpResponse.json(data.storyboardStatus);
      if (!data.scriptDetail) return new HttpResponse(null, { status: 404 });
      return HttpResponse.json(
        storyboardStatus({
          script: data.scriptDetail,
          segments: data.scriptDetail.segments.map((segment) =>
            segmentStoryboard({
              id: segment.id,
              segment_index: segment.segment_index,
              audio_measured_duration_ms: data.scriptDetail?.measured_duration_ms ?? null,
            }),
          ),
          not_ready_segment_ids: data.scriptDetail.segments.map((segment) => segment.id),
        }),
      );
    }),
    http.get(`${base}/claim-verifications`, () => HttpResponse.json(page(data.claims ?? []))),
  );
}

/** Render a project page inside the real workspace shell, as the app does. */
export function renderProjectPage(page_: ReactElement, path: 'topics' | 'script' | '' = '') {
  navigation.projectId = PROJECT_ID;
  navigation.pathname = `/projects/${PROJECT_ID}${path ? `/${path}` : ''}`;
  return renderWithClient(<WorkspaceShell>{page_}</WorkspaceShell>);
}
