'use client';

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationOptions,
} from '@tanstack/react-query';
import { useEffect, useRef } from 'react';
import type {
  Job,
  Project,
  ProjectCreateRequest,
  ProjectUpdateRequest,
  Script,
  Topic,
  TopicUpdateRequest,
} from '@edutoon/contracts';
import { isActiveJob } from '@/lib/jobs';
import { currentScript, deriveWorkflow, type Workflow } from '@/lib/workflow';
import { newIdempotencyKey } from './client';
import { ApiError } from './errors';
import * as api from './endpoints';

/** How often a screen re-checks work that is running in the background. */
export const POLL_INTERVAL_MS = 3_000;

export const queryKeys = {
  projects: ['projects'] as const,
  projectList: ['projects', 'list'] as const,
  project: (id: string) => ['projects', id] as const,
  sources: (id: string) => ['projects', id, 'sources'] as const,
  chunks: (id: string, sourceId: string) =>
    ['projects', id, 'sources', sourceId, 'chunks'] as const,
  topics: (id: string) => ['projects', id, 'topics'] as const,
  jobs: (id: string) => ['projects', id, 'jobs'] as const,
  scripts: (id: string) => ['projects', id, 'scripts'] as const,
  script: (id: string, scriptId: string) => ['projects', id, 'scripts', scriptId] as const,
  synthesis: (id: string, scriptId: string) =>
    ['projects', id, 'scripts', scriptId, 'synthesis'] as const,
  storyboard: (id: string, scriptId: string) =>
    ['projects', id, 'scripts', scriptId, 'storyboard'] as const,
  claims: (id: string) => ['projects', id, 'claims'] as const,
};

// --- reads ------------------------------------------------------------------------------

export const PROJECT_PAGE_SIZE = 20;

export function useProjectList() {
  return useInfiniteQuery({
    queryKey: queryKeys.projectList,
    queryFn: ({ pageParam }) => api.listProjects({ cursor: pageParam, limit: PROJECT_PAGE_SIZE }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.nextCursor,
  });
}

export function useProject(projectId: string) {
  return useQuery({
    queryKey: queryKeys.project(projectId),
    queryFn: () => api.getProject(projectId),
  });
}

export function useSources(projectId: string) {
  return useQuery({
    queryKey: queryKeys.sources(projectId),
    queryFn: () => api.listSources(projectId),
  });
}

export function useTopics(projectId: string) {
  return useQuery({
    queryKey: queryKeys.topics(projectId),
    queryFn: () => api.listTopics(projectId),
  });
}

/** The project's recent jobs, polled for as long as one of them is queued or running. */
export function useJobs(projectId: string) {
  return useQuery({
    queryKey: queryKeys.jobs(projectId),
    queryFn: () => api.listJobs(projectId),
    refetchInterval: (query) => (query.state.data?.some(isActiveJob) ? POLL_INTERVAL_MS : false),
  });
}

/** The project's scripts, polled while the newest is still being written. */
export function useScripts(projectId: string) {
  return useQuery({
    queryKey: queryKeys.scripts(projectId),
    queryFn: () => api.listScripts(projectId),
    refetchInterval: (query) =>
      currentScript(query.state.data)?.status === 'pending' ? POLL_INTERVAL_MS : false,
  });
}

export function useScriptDetail(projectId: string, scriptId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.script(projectId, scriptId ?? ''),
    queryFn: () => api.getScript(projectId, scriptId as string),
    enabled: scriptId !== undefined,
  });
}

/** A script's synthesis status, polled while its most recent job is still queued or running. */
export function useSynthesisStatus(projectId: string, scriptId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.synthesis(projectId, scriptId ?? ''),
    queryFn: () => api.getSynthesisStatus(projectId, scriptId as string),
    enabled: scriptId !== undefined,
    refetchInterval: (query) => {
      const job = query.state.data?.job;
      return job && isActiveJob(job) ? POLL_INTERVAL_MS : false;
    },
  });
}

/** A script's storyboard status, polled while its most recent job is still queued or running. */
export function useStoryboardStatus(projectId: string, scriptId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.storyboard(projectId, scriptId ?? ''),
    queryFn: () => api.getStoryboardStatus(projectId, scriptId as string),
    enabled: scriptId !== undefined,
    refetchInterval: (query) => {
      const job = query.state.data?.job;
      return job && isActiveJob(job) ? POLL_INTERVAL_MS : false;
    },
  });
}

/** Every claim verification the project has, manual and extracted alike. */
export function useClaimVerifications(projectId: string) {
  return useQuery({
    queryKey: queryKeys.claims(projectId),
    queryFn: () => api.listClaimVerifications(projectId),
  });
}

/** Whether the project has any claim verified as supported - what a script may be written from. */
export function useSupportedClaimCount(projectId: string) {
  return useQuery({
    queryKey: queryKeys.claims(projectId),
    queryFn: () => api.listClaimVerifications(projectId),
    select: (claims) => claims.filter((claim) => claim.status === 'supported').length,
  });
}

/** The text of a source's chunks, for showing the passage a topic was drawn from. */
export function useSourceChunks(projectId: string, sourceId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.chunks(projectId, sourceId ?? ''),
    queryFn: () => api.listSourceChunks(projectId, sourceId as string),
    enabled: sourceId !== undefined,
    staleTime: 10 * 60_000,
  });
}

// --- the workspace ----------------------------------------------------------------------

export interface Workspace {
  project: Project | undefined;
  workflow: Workflow | undefined;
  sources: ReturnType<typeof useSources>;
  topics: ReturnType<typeof useTopics>;
  jobs: ReturnType<typeof useJobs>;
  scripts: ReturnType<typeof useScripts>;
  claims: ReturnType<typeof useClaimVerifications>;
  /** The project itself could not be loaded. */
  error: unknown;
  isLoading: boolean;
}

/**
 * Everything one project's screens need, and the workflow derived from it. The queries are
 * shared through the cache, so calling this from a layout and from a page costs nothing extra.
 */
export function useWorkspace(projectId: string): Workspace {
  const projectQuery = useProject(projectId);
  const sources = useSources(projectId);
  const topics = useTopics(projectId);
  const jobs = useJobs(projectId);
  const scripts = useScripts(projectId);
  const claims = useClaimVerifications(projectId);
  const queryClient = useQueryClient();

  // When a background job finishes, what it produced is now stored: fetch it straight away
  // instead of waiting for the next scheduled refresh.
  const jobSignature = (jobs.data ?? []).map((job) => `${job.id}:${job.status}`).join('|');
  const settled = useRef<string | null>(null);
  useEffect(() => {
    if (jobs.data === undefined) return;
    if (settled.current !== null && settled.current !== jobSignature) {
      void queryClient.invalidateQueries({ queryKey: queryKeys.topics(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.scripts(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.sources(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.claims(projectId) });
    }
    settled.current = jobSignature;
  }, [jobSignature, jobs.data, projectId, queryClient]);

  const project = projectQuery.data;
  const workflow = project
    ? deriveWorkflow({
        project,
        sources: sources.data,
        topics: topics.data,
        jobs: jobs.data,
        scripts: scripts.data,
      })
    : undefined;

  return {
    project,
    workflow,
    sources,
    topics,
    jobs,
    scripts,
    claims,
    error: projectQuery.error,
    isLoading: projectQuery.isPending,
  };
}

/**
 * Just enough to label a project in a list: its scripts and jobs. Two small requests, only for
 * the rows being shown, and cached so opening the project afterwards is instant.
 */
export function useProjectStage(project: Project): Workflow {
  const jobs = useJobs(project.id);
  const scripts = useScripts(project.id);
  return deriveWorkflow({ project, jobs: jobs.data, scripts: scripts.data });
}

// --- writes -----------------------------------------------------------------------------

/**
 * A mutation that sends an `Idempotency-Key`, and keeps sending the *same* one when the person
 * retries the *same* request after an outcome that is unknown (a timeout, a dropped connection,
 * a server fault) - so a replay is deduplicated by the API instead of doing the work twice.
 * A different request, a definite rejection, or a success all start with a fresh key.
 */
export function useIdempotentMutation<TVariables, TData>(
  mutationFn: (variables: TVariables, key: string) => Promise<TData>,
  options: Omit<UseMutationOptions<TData, Error, TVariables>, 'mutationFn'> = {},
) {
  const attempt = useRef<{ signature: string; key: string } | null>(null);

  return useMutation<TData, Error, TVariables>({
    ...options,
    mutationFn: async (variables) => {
      const signature = signatureOf(variables);
      if (attempt.current?.signature !== signature) {
        attempt.current = { signature, key: newIdempotencyKey() };
      }
      const { key } = attempt.current;
      try {
        const data = await mutationFn(variables, key);
        attempt.current = null;
        return data;
      } catch (error) {
        if (!(error instanceof ApiError && error.outcomeUnknown)) attempt.current = null;
        throw error;
      }
    },
  });
}

/** Identifies "the same request". `String(...)` because `JSON.stringify(undefined)` is not a string. */
export function signatureOf(variables: unknown): string {
  return String(
    JSON.stringify(variables, (_key, value) =>
      typeof File !== 'undefined' && value instanceof File
        ? `file:${value.name}:${value.size}:${value.lastModified}`
        : value,
    ),
  );
}

export function useCreateProject() {
  const queryClient = useQueryClient();
  return useIdempotentMutation((body: ProjectCreateRequest, key) => api.createProject(body, key), {
    onSuccess: (project) => {
      queryClient.setQueryData(queryKeys.project(project.id), project);
      void queryClient.invalidateQueries({ queryKey: queryKeys.projectList });
    },
  });
}

export function useUpdateProject(projectId: string) {
  const queryClient = useQueryClient();
  return useIdempotentMutation(
    (body: ProjectUpdateRequest, key) => api.updateProject(projectId, body, key),
    {
      onSuccess: (project) => {
        queryClient.setQueryData(queryKeys.project(projectId), project);
        void queryClient.invalidateQueries({ queryKey: queryKeys.projectList });
      },
    },
  );
}

export function useDeleteProject(projectId: string) {
  const queryClient = useQueryClient();
  return useIdempotentMutation((_: void, key) => api.deleteProject(projectId, key), {
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: queryKeys.project(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.projectList });
    },
  });
}

export function useUploadSource(projectId: string) {
  const queryClient = useQueryClient();
  return useIdempotentMutation((file: File, key) => api.uploadSource(projectId, file, key), {
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.sources(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.jobs(projectId) });
    },
  });
}

export function useAddKeywords(projectId: string) {
  const queryClient = useQueryClient();
  return useIdempotentMutation(
    (keywords: string[], key) => api.addKeywords(projectId, keywords, key),
    {
      onSuccess: () => {
        void queryClient.invalidateQueries({ queryKey: queryKeys.topics(projectId) });
      },
    },
  );
}

export function useStartExtraction(projectId: string) {
  const queryClient = useQueryClient();
  return useIdempotentMutation(
    (options: { replace: boolean }, key) => api.startTopicExtraction(projectId, options, key),
    {
      onSuccess: (job: Job) => {
        // Show the new job at once, so the screen switches to "in progress" without a gap.
        queryClient.setQueryData<Job[]>(queryKeys.jobs(projectId), (jobs) => [
          job,
          ...(jobs ?? []).filter((existing) => existing.id !== job.id),
        ]);
        void queryClient.invalidateQueries({ queryKey: queryKeys.jobs(projectId) });
      },
    },
  );
}

/**
 * Turn the project's accepted topics into candidate claims and verify each one (Phase 6).
 * Additive: calling it again after accepting more topics covers only the new ones, so this
 * always sends `replace: false` - the ordinary, idempotent way to extend coverage.
 */
export function useStartClaimExtraction(projectId: string) {
  const queryClient = useQueryClient();
  return useIdempotentMutation(
    (_: void, key) => api.startClaimExtraction(projectId, { replace: false }, key),
    {
      onSuccess: (job: Job) => {
        // Show the new job at once, so the screen switches to "in progress" without a gap.
        queryClient.setQueryData<Job[]>(queryKeys.jobs(projectId), (jobs) => [
          job,
          ...(jobs ?? []).filter((existing) => existing.id !== job.id),
        ]);
        void queryClient.invalidateQueries({ queryKey: queryKeys.jobs(projectId) });
      },
    },
  );
}

interface TopicEdit {
  topicId: string;
  patch: TopicUpdateRequest;
}

export function useUpdateTopic(projectId: string) {
  const queryClient = useQueryClient();
  const key = queryKeys.topics(projectId);
  return useIdempotentMutation(
    ({ topicId, patch }: TopicEdit, idempotencyKey) =>
      api.updateTopic(projectId, topicId, patch, idempotencyKey),
    {
      // Accepting or rejecting should feel instant; roll back if the API says no.
      onMutate: async ({ topicId, patch }) => {
        await queryClient.cancelQueries({ queryKey: key });
        const previous = queryClient.getQueryData<Topic[]>(key);
        queryClient.setQueryData<Topic[]>(key, (topics) =>
          topics?.map((topic) => (topic.id === topicId ? applyTopicPatch(topic, patch) : topic)),
        );
        return { previous };
      },
      onError: (_error, _variables, context) => {
        const previous = (context as { previous?: Topic[] } | undefined)?.previous;
        if (previous) queryClient.setQueryData(key, previous);
      },
      onSettled: () => {
        void queryClient.invalidateQueries({ queryKey: key });
      },
    },
  );
}

function applyTopicPatch(topic: Topic, patch: TopicUpdateRequest): Topic {
  return {
    ...topic,
    ...(patch.status != null ? { status: patch.status } : {}),
    ...(patch.title != null ? { title: patch.title } : {}),
    ...(patch.summary !== undefined ? { summary: patch.summary } : {}),
  };
}

export function useGenerateScript(projectId: string) {
  const queryClient = useQueryClient();
  return useIdempotentMutation((_: void, key) => api.generateScript(projectId, key), {
    onSuccess: (script: Script) => {
      // The API supersedes whatever script was live when it queues a new one; mirror that now.
      queryClient.setQueryData<Script[]>(queryKeys.scripts(projectId), (scripts) => [
        script,
        ...(scripts ?? [])
          .filter((existing) => existing.id !== script.id)
          .map((existing) => ({ ...existing, status: 'superseded' as const })),
      ]);
      void queryClient.invalidateQueries({ queryKey: queryKeys.scripts(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.jobs(projectId) });
    },
  });
}

export function useApproveScript(projectId: string) {
  const queryClient = useQueryClient();
  return useIdempotentMutation(
    (scriptId: string, key) => api.approveScript(projectId, scriptId, key),
    {
      onSuccess: () => {
        void queryClient.invalidateQueries({ queryKey: queryKeys.scripts(projectId) });
      },
    },
  );
}

export function useEnqueueSynthesis(projectId: string, scriptId: string) {
  const queryClient = useQueryClient();
  return useIdempotentMutation((_: void, key) => api.enqueueSynthesis(projectId, scriptId, key), {
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.synthesis(projectId, scriptId) });
    },
  });
}

export function useEnqueueStoryboard(projectId: string, scriptId: string) {
  const queryClient = useQueryClient();
  return useIdempotentMutation((_: void, key) => api.enqueueStoryboard(projectId, scriptId, key), {
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.storyboard(projectId, scriptId) });
    },
  });
}
