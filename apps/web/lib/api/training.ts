import type {
  AdminAssessment,
  Attempt,
  AttemptSubmitRequest,
  AssessmentPutRequest,
  Course,
  Enrolment,
  EmployeeAssessment,
  Me,
  Module,
  ModulePublishRequest,
  MyCourse,
  UserSummary,
} from '@edutoon/contracts';
import { getApiToken } from '@/lib/auth/token';
import { API_BASE_URL, getApiClient, timeoutSignal, UPLOAD_TIMEOUT_MS, unwrap } from './client';
import { apiErrorFromResponse } from './errors';

/**
 * The training platform's routes: who the signed-in person is, an employee's own courses, modules
 * and assessments, and an administrator's course, module, assessment and enrolment management.
 * One function per route, typed from the API's own contract; every mutation takes the
 * `Idempotency-Key` the caller chose.
 */

const idempotent = (key: string) => ({ 'idempotency-key': key });

// --- who am I ---------------------------------------------------------------------------

/** The signed-in person and the role the database gives them: the only way a client learns it. */
export function getMe(): Promise<Me> {
  return unwrap(getApiClient().GET('/v1/auth/me', { signal: timeoutSignal() }));
}

// --- an employee's own learning ---------------------------------------------------------

export async function listMyCourses(): Promise<MyCourse[]> {
  const data = await unwrap(
    getApiClient().GET('/v1/training/courses', { signal: timeoutSignal() }),
  );
  return data.items;
}

export function getMyAssessment(moduleId: string): Promise<EmployeeAssessment> {
  return unwrap(
    getApiClient().GET('/v1/training/modules/{module_id}/assessment', {
      params: { path: { module_id: moduleId } },
      signal: timeoutSignal(),
    }),
  );
}

export async function listMyAttempts(moduleId: string): Promise<Attempt[]> {
  const data = await unwrap(
    getApiClient().GET('/v1/training/modules/{module_id}/assessment/attempts', {
      params: { path: { module_id: moduleId } },
      signal: timeoutSignal(),
    }),
  );
  return data.items;
}

export function submitAttempt(
  moduleId: string,
  body: AttemptSubmitRequest,
  key: string,
): Promise<Attempt> {
  return unwrap(
    getApiClient().POST('/v1/training/modules/{module_id}/assessment/attempts', {
      params: { path: { module_id: moduleId }, header: idempotent(key) },
      body,
      signal: timeoutSignal(),
    }),
  );
}

async function fetchVideoUrl(path: string): Promise<string> {
  const token = await getApiToken();
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    signal: timeoutSignal(UPLOAD_TIMEOUT_MS),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => undefined);
    throw apiErrorFromResponse(response, body);
  }
  return URL.createObjectURL(await response.blob());
}

/** A playable `blob:` URL for a module the employee may open (the caller must revoke it). */
export const fetchModuleVideoUrl = (moduleId: string): Promise<string> =>
  fetchVideoUrl(`/v1/training/modules/${moduleId}/video`);

/** An administrator's preview of a module's own copy of its video. */
export const fetchModulePreviewUrl = (moduleId: string): Promise<string> =>
  fetchVideoUrl(`/v1/admin/training/modules/${moduleId}/video`);

// --- an administrator: courses ----------------------------------------------------------

export async function listCourses(): Promise<Course[]> {
  const data = await unwrap(
    getApiClient().GET('/v1/admin/training/courses', {
      params: { query: { limit: 100 } },
      signal: timeoutSignal(),
    }),
  );
  return data.items;
}

export function getCourse(courseId: string): Promise<Course> {
  return unwrap(
    getApiClient().GET('/v1/admin/training/courses/{course_id}', {
      params: { path: { course_id: courseId } },
      signal: timeoutSignal(),
    }),
  );
}

export function createCourse(
  body: { title: string; description?: string | null },
  key: string,
): Promise<Course> {
  return unwrap(
    getApiClient().POST('/v1/admin/training/courses', {
      params: { header: idempotent(key) },
      body,
      signal: timeoutSignal(),
    }),
  );
}

export function publishCourse(courseId: string, key: string): Promise<Course> {
  return unwrap(
    getApiClient().POST('/v1/admin/training/courses/{course_id}/publish', {
      params: { path: { course_id: courseId }, header: idempotent(key) },
      signal: timeoutSignal(),
    }),
  );
}

export function archiveCourse(courseId: string, key: string): Promise<Course> {
  return unwrap(
    getApiClient().POST('/v1/admin/training/courses/{course_id}/archive', {
      params: { path: { course_id: courseId }, header: idempotent(key) },
      signal: timeoutSignal(),
    }),
  );
}

// --- an administrator: modules ----------------------------------------------------------

export async function listModules(courseId: string): Promise<Module[]> {
  const data = await unwrap(
    getApiClient().GET('/v1/admin/training/courses/{course_id}/modules', {
      params: { path: { course_id: courseId } },
      signal: timeoutSignal(),
    }),
  );
  return data.items;
}

export function publishModule(
  courseId: string,
  body: ModulePublishRequest,
  key: string,
): Promise<Module> {
  return unwrap(
    getApiClient().POST('/v1/admin/training/courses/{course_id}/modules', {
      params: { path: { course_id: courseId }, header: idempotent(key) },
      body,
      // Publishing copies the video into the module's own storage: allow it time.
      signal: timeoutSignal(UPLOAD_TIMEOUT_MS),
    }),
  );
}

export function unpublishModule(moduleId: string, key: string): Promise<Module> {
  return unwrap(
    getApiClient().POST('/v1/admin/training/modules/{module_id}/unpublish', {
      params: { path: { module_id: moduleId }, header: idempotent(key) },
      signal: timeoutSignal(),
    }),
  );
}

export async function deleteModule(moduleId: string, key: string): Promise<void> {
  await unwrap(
    getApiClient().DELETE('/v1/admin/training/modules/{module_id}', {
      params: { path: { module_id: moduleId }, header: idempotent(key) },
      signal: timeoutSignal(),
    }),
  );
}

export async function reorderModules(
  courseId: string,
  moduleIds: string[],
  key: string,
): Promise<Module[]> {
  const data = await unwrap(
    getApiClient().PUT('/v1/admin/training/courses/{course_id}/modules/order', {
      params: { path: { course_id: courseId }, header: idempotent(key) },
      body: { module_ids: moduleIds },
      signal: timeoutSignal(),
    }),
  );
  return data.items;
}

// --- an administrator: assessments ------------------------------------------------------

export function getAdminAssessment(moduleId: string): Promise<AdminAssessment> {
  return unwrap(
    getApiClient().GET('/v1/admin/training/modules/{module_id}/assessment', {
      params: { path: { module_id: moduleId } },
      signal: timeoutSignal(),
    }),
  );
}

export function putAssessment(
  moduleId: string,
  body: AssessmentPutRequest,
  key: string,
): Promise<AdminAssessment> {
  return unwrap(
    getApiClient().PUT('/v1/admin/training/modules/{module_id}/assessment', {
      params: { path: { module_id: moduleId }, header: idempotent(key) },
      body,
      signal: timeoutSignal(),
    }),
  );
}

export function publishAssessment(moduleId: string, key: string): Promise<AdminAssessment> {
  return unwrap(
    getApiClient().POST('/v1/admin/training/modules/{module_id}/assessment/publish', {
      params: { path: { module_id: moduleId }, header: idempotent(key) },
      signal: timeoutSignal(),
    }),
  );
}

// --- an administrator: people and enrolments --------------------------------------------

export async function listUsers(): Promise<UserSummary[]> {
  const data = await unwrap(
    getApiClient().GET('/v1/admin/users', {
      params: { query: { limit: 100 } },
      signal: timeoutSignal(),
    }),
  );
  return data.items;
}

export async function listEnrolments(courseId: string): Promise<Enrolment[]> {
  const data = await unwrap(
    getApiClient().GET('/v1/admin/training/courses/{course_id}/enrolments', {
      params: { path: { course_id: courseId }, query: { limit: 100 } },
      signal: timeoutSignal(),
    }),
  );
  return data.items;
}

export function enrolUser(courseId: string, userId: string, key: string): Promise<Enrolment> {
  return unwrap(
    getApiClient().POST('/v1/admin/training/courses/{course_id}/enrolments', {
      params: { path: { course_id: courseId }, header: idempotent(key) },
      body: { user_id: userId },
      signal: timeoutSignal(),
    }),
  );
}

export function withdrawUser(courseId: string, userId: string, key: string): Promise<Enrolment> {
  return unwrap(
    getApiClient().POST('/v1/admin/training/courses/{course_id}/enrolments/{user_id}/withdraw', {
      params: { path: { course_id: courseId, user_id: userId }, header: idempotent(key) },
      signal: timeoutSignal(),
    }),
  );
}

export function inviteUser(
  body: { email: string; role: 'employee' | 'admin'; display_name?: string | null },
  key: string,
) {
  return unwrap(
    getApiClient().POST('/v1/admin/invitations', {
      params: { header: idempotent(key) },
      body,
      signal: timeoutSignal(),
    }),
  );
}
