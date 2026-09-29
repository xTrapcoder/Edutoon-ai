import type {
  ClaimVerification,
  Job,
  Orientation,
  Project,
  ProjectCreateRequest,
  ProjectUpdateRequest,
  RenderEnqueueResponse,
  RenderProgressStatus,
  RenderQuality,
  RenderStatus,
  Script,
  ScriptDetail,
  Source,
  SourceChunk,
  StoryboardEnqueueResponse,
  StoryboardStatus,
  SynthesisStatus,
  Topic,
  TopicUpdateRequest,
} from '@edutoon/contracts';
import { getApiToken } from '@/lib/auth/token';
import { API_BASE_URL, getApiClient, timeoutSignal, UPLOAD_TIMEOUT_MS, unwrap } from './client';
import { apiErrorFromResponse } from './errors';

/**
 * One function per API route the product uses, each returning the API's own typed response.
 * Every mutating call takes the `Idempotency-Key` the caller chose (rule 8 of the API).
 */

export interface Page<T> {
  items: T[];
  nextCursor: string | null;
}

const idempotent = (key: string) => ({ 'idempotency-key': key });

// --- projects ---------------------------------------------------------------------------

export async function listProjects(options: { cursor?: string | null; limit?: number } = {}) {
  const data = await unwrap(
    getApiClient().GET('/v1/projects', {
      params: { query: { limit: options.limit, cursor: options.cursor ?? undefined } },
      signal: timeoutSignal(),
    }),
  );
  return { items: data.items, nextCursor: data.next_cursor } satisfies Page<Project>;
}

export function getProject(projectId: string): Promise<Project> {
  return unwrap(
    getApiClient().GET('/v1/projects/{project_id}', {
      params: { path: { project_id: projectId } },
      signal: timeoutSignal(),
    }),
  );
}

export function createProject(body: ProjectCreateRequest, key: string): Promise<Project> {
  return unwrap(
    getApiClient().POST('/v1/projects', {
      params: { header: idempotent(key) },
      body,
      signal: timeoutSignal(),
    }),
  );
}

export function updateProject(
  projectId: string,
  body: ProjectUpdateRequest,
  key: string,
): Promise<Project> {
  return unwrap(
    getApiClient().PATCH('/v1/projects/{project_id}', {
      params: { path: { project_id: projectId }, header: idempotent(key) },
      body,
      signal: timeoutSignal(),
    }),
  );
}

export async function deleteProject(projectId: string, key: string): Promise<void> {
  await unwrap(
    getApiClient().DELETE('/v1/projects/{project_id}', {
      params: { path: { project_id: projectId }, header: idempotent(key) },
      signal: timeoutSignal(),
    }),
  );
}

// --- sources ----------------------------------------------------------------------------

export async function listSources(projectId: string): Promise<Source[]> {
  const data = await unwrap(
    getApiClient().GET('/v1/projects/{project_id}/sources', {
      params: { path: { project_id: projectId }, query: { limit: 20 } },
      signal: timeoutSignal(),
    }),
  );
  return data.items;
}

export function uploadSource(projectId: string, file: File, key: string): Promise<Source> {
  return unwrap(
    getApiClient().POST('/v1/projects/{project_id}/sources', {
      params: { path: { project_id: projectId }, header: idempotent(key) },
      // The generated type describes the multipart body as `{ file: string }`; the real payload
      // is built by the serializer below so the browser sets the multipart boundary itself.
      body: { file: '' },
      bodySerializer: () => {
        const form = new FormData();
        form.append('file', file);
        return form;
      },
      signal: timeoutSignal(UPLOAD_TIMEOUT_MS),
    }),
  );
}

/** Every chunk of one source, in reading order, with its text. */
export async function listSourceChunks(
  projectId: string,
  sourceId: string,
): Promise<SourceChunk[]> {
  const data = await unwrap(
    getApiClient().GET('/v1/projects/{project_id}/sources/{source_id}/chunks', {
      params: { path: { project_id: projectId, source_id: sourceId } },
      signal: timeoutSignal(),
    }),
  );
  return data.items;
}

// --- topics -----------------------------------------------------------------------------

export async function listTopics(projectId: string): Promise<Topic[]> {
  const data = await unwrap(
    getApiClient().GET('/v1/projects/{project_id}/topics', {
      params: { path: { project_id: projectId } },
      signal: timeoutSignal(),
    }),
  );
  return data.items;
}

export async function addKeywords(
  projectId: string,
  keywords: string[],
  key: string,
): Promise<Topic[]> {
  const data = await unwrap(
    getApiClient().POST('/v1/projects/{project_id}/topics', {
      params: { path: { project_id: projectId }, header: idempotent(key) },
      body: { keywords },
      signal: timeoutSignal(),
    }),
  );
  return data.items;
}

export function startTopicExtraction(
  projectId: string,
  options: { replace: boolean },
  key: string,
): Promise<Job> {
  return unwrap(
    getApiClient().POST('/v1/projects/{project_id}/topics/extraction', {
      params: { path: { project_id: projectId }, header: idempotent(key) },
      body: { replace: options.replace },
      signal: timeoutSignal(),
    }),
  );
}

export function updateTopic(
  projectId: string,
  topicId: string,
  body: TopicUpdateRequest,
  key: string,
): Promise<Topic> {
  return unwrap(
    getApiClient().PATCH('/v1/projects/{project_id}/topics/{topic_id}', {
      params: { path: { project_id: projectId, topic_id: topicId }, header: idempotent(key) },
      body,
      signal: timeoutSignal(),
    }),
  );
}

// --- jobs -------------------------------------------------------------------------------

/** The project's most recent jobs, newest first. */
export async function listJobs(projectId: string, limit = 20): Promise<Job[]> {
  const data = await unwrap(
    getApiClient().GET('/v1/projects/{project_id}/jobs', {
      params: { path: { project_id: projectId }, query: { limit } },
      signal: timeoutSignal(),
    }),
  );
  return data.items;
}

// --- claims -----------------------------------------------------------------------------

export async function listClaimVerifications(
  projectId: string,
  limit = 100,
): Promise<ClaimVerification[]> {
  const data = await unwrap(
    getApiClient().GET('/v1/projects/{project_id}/claim-verifications', {
      params: { path: { project_id: projectId }, query: { limit } },
      signal: timeoutSignal(),
    }),
  );
  return data.items;
}

export function startClaimExtraction(
  projectId: string,
  options: { replace: boolean },
  key: string,
): Promise<Job> {
  return unwrap(
    getApiClient().POST('/v1/projects/{project_id}/claim-verifications/extraction', {
      params: { path: { project_id: projectId }, header: idempotent(key) },
      body: { replace: options.replace },
      signal: timeoutSignal(),
    }),
  );
}

// --- scripts ----------------------------------------------------------------------------

/** The project's scripts, newest first (superseded ones included). */
export async function listScripts(projectId: string, limit = 10): Promise<Script[]> {
  const data = await unwrap(
    getApiClient().GET('/v1/projects/{project_id}/scripts', {
      params: { path: { project_id: projectId }, query: { limit } },
      signal: timeoutSignal(),
    }),
  );
  return data.items;
}

export function getScript(projectId: string, scriptId: string): Promise<ScriptDetail> {
  return unwrap(
    getApiClient().GET('/v1/projects/{project_id}/scripts/{script_id}', {
      params: { path: { project_id: projectId, script_id: scriptId } },
      signal: timeoutSignal(),
    }),
  );
}

export function generateScript(projectId: string, key: string): Promise<Script> {
  return unwrap(
    getApiClient().POST('/v1/projects/{project_id}/scripts', {
      params: { path: { project_id: projectId }, header: idempotent(key) },
      signal: timeoutSignal(),
    }),
  );
}

export function approveScript(projectId: string, scriptId: string, key: string): Promise<Script> {
  return unwrap(
    getApiClient().POST('/v1/projects/{project_id}/scripts/{script_id}/approval', {
      params: { path: { project_id: projectId, script_id: scriptId }, header: idempotent(key) },
      signal: timeoutSignal(),
    }),
  );
}

// --- synthesis (Phase 8) -----------------------------------------------------------------

export function enqueueSynthesis(projectId: string, scriptId: string, key: string): Promise<Job> {
  return unwrap(
    getApiClient().POST('/v1/projects/{project_id}/scripts/{script_id}/synthesis', {
      params: { path: { project_id: projectId, script_id: scriptId }, header: idempotent(key) },
      signal: timeoutSignal(),
    }),
  );
}

export function getSynthesisStatus(projectId: string, scriptId: string): Promise<SynthesisStatus> {
  return unwrap(
    getApiClient().GET('/v1/projects/{project_id}/scripts/{script_id}/synthesis', {
      params: { path: { project_id: projectId, script_id: scriptId } },
      signal: timeoutSignal(),
    }),
  );
}

// --- storyboard (Phase 9) ----------------------------------------------------------------

export function enqueueStoryboard(
  projectId: string,
  scriptId: string,
  key: string,
): Promise<StoryboardEnqueueResponse> {
  return unwrap(
    getApiClient().POST('/v1/projects/{project_id}/scripts/{script_id}/storyboard', {
      params: { path: { project_id: projectId, script_id: scriptId }, header: idempotent(key) },
      signal: timeoutSignal(),
    }),
  );
}

export function getStoryboardStatus(
  projectId: string,
  scriptId: string,
): Promise<StoryboardStatus> {
  return unwrap(
    getApiClient().GET('/v1/projects/{project_id}/scripts/{script_id}/storyboard', {
      params: { path: { project_id: projectId, script_id: scriptId } },
      signal: timeoutSignal(),
    }),
  );
}

// --- video rendering (Phase 10) ---------------------------------------------------------

export function enqueueRender(
  projectId: string,
  scriptId: string,
  body: { orientation: Orientation; quality: RenderQuality },
  key: string,
): Promise<RenderEnqueueResponse> {
  return unwrap(
    getApiClient().POST('/v1/projects/{project_id}/scripts/{script_id}/render', {
      params: { path: { project_id: projectId, script_id: scriptId }, header: idempotent(key) },
      body,
      signal: timeoutSignal(),
    }),
  );
}

export function retryRender(projectId: string, key: string): Promise<RenderEnqueueResponse> {
  return unwrap(
    getApiClient().POST('/v1/projects/{project_id}/render/retry', {
      params: { path: { project_id: projectId }, header: idempotent(key) },
      signal: timeoutSignal(),
    }),
  );
}

export function getRenderStatus(
  projectId: string,
  scriptId: string,
  orientation: Orientation,
  quality: RenderQuality,
): Promise<RenderStatus> {
  return unwrap(
    getApiClient().GET('/v1/projects/{project_id}/scripts/{script_id}/render', {
      params: {
        path: { project_id: projectId, script_id: scriptId },
        query: { orientation, quality },
      },
      signal: timeoutSignal(),
    }),
  );
}

/** The cheap poll: the project's latest render job and its live progress. */
export function getRenderProgress(projectId: string): Promise<RenderProgressStatus> {
  return unwrap(
    getApiClient().GET('/v1/projects/{project_id}/render/progress', {
      params: { path: { project_id: projectId } },
      signal: timeoutSignal(),
    }),
  );
}

/**
 * A playable, same-origin object URL for a finished video (see `fetchSegmentAudioUrl` for why a
 * `blob:` URL: a plain `<video src>` cannot carry the `Authorization` header). The API streams
 * the file and honours `Range` requests; this reads it whole, so the player can seek freely once
 * it has loaded. The caller owns the URL and must revoke it when it is no longer shown.
 */
export async function fetchRenderVideoUrl(
  projectId: string,
  scriptId: string,
  orientation: Orientation,
  quality: RenderQuality,
): Promise<string> {
  const token = await getApiToken();
  const response = await fetch(
    `${API_BASE_URL}/v1/projects/${projectId}/scripts/${scriptId}/render/video` +
      `?orientation=${orientation}&quality=${quality}`,
    {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      signal: timeoutSignal(UPLOAD_TIMEOUT_MS),
    },
  );
  if (!response.ok) {
    const body = await response.json().catch(() => undefined);
    throw apiErrorFromResponse(response, body);
  }
  return URL.createObjectURL(await response.blob());
}

/**
 * A playable, same-origin object URL for one segment's current audio.
 *
 * A plain `<audio src="...">` cannot carry the `Authorization` header the API requires, so this
 * fetches the audio itself (with the caller's token, like every other request) and hands back a
 * `blob:` URL instead of the bare API URL. The caller owns the URL's lifetime and must revoke it
 * (`URL.revokeObjectURL`) once it is no longer shown, typically when the player unmounts.
 */
export async function fetchSegmentAudioUrl(
  projectId: string,
  scriptId: string,
  segmentId: string,
): Promise<string> {
  const token = await getApiToken();
  const response = await fetch(
    `${API_BASE_URL}/v1/projects/${projectId}/scripts/${scriptId}/segments/${segmentId}/audio`,
    {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      signal: timeoutSignal(UPLOAD_TIMEOUT_MS),
    },
  );
  if (!response.ok) {
    const body = await response.json().catch(() => undefined);
    throw apiErrorFromResponse(response, body);
  }
  const blob = await response.blob();
  return URL.createObjectURL(blob);
}
