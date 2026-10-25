'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import type {
  AssessmentPutRequest,
  AttemptSubmitRequest,
  ModulePublishRequest,
} from '@edutoon/contracts';
import * as training from './training';
import { useIdempotentMutation } from './queries';

export const trainingKeys = {
  me: ['me'] as const,
  myCourses: ['training', 'my-courses'] as const,
  myAssessment: (moduleId: string) => ['training', 'my-assessment', moduleId] as const,
  myAttempts: (moduleId: string) => ['training', 'my-attempts', moduleId] as const,
  courses: ['training', 'courses'] as const,
  course: (courseId: string) => ['training', 'courses', courseId] as const,
  modules: (courseId: string) => ['training', 'courses', courseId, 'modules'] as const,
  assessment: (moduleId: string) => ['training', 'assessment', moduleId] as const,
  enrolments: (courseId: string) => ['training', 'courses', courseId, 'enrolments'] as const,
  users: ['training', 'users'] as const,
};

// --- who am I ---------------------------------------------------------------------------

/** The signed-in person and their role. The role comes from the database, never from a token. */
export function useMe() {
  return useQuery({ queryKey: trainingKeys.me, queryFn: training.getMe, staleTime: 60_000 });
}

// --- an employee ------------------------------------------------------------------------

export function useMyCourses() {
  return useQuery({ queryKey: trainingKeys.myCourses, queryFn: training.listMyCourses });
}

export function useMyAssessment(moduleId: string, enabled: boolean) {
  return useQuery({
    queryKey: trainingKeys.myAssessment(moduleId),
    queryFn: () => training.getMyAssessment(moduleId),
    enabled,
    // A module with no assessment is an ordinary answer (404), not worth retrying.
    retry: false,
  });
}

export function useMyAttempts(moduleId: string, enabled: boolean) {
  return useQuery({
    queryKey: trainingKeys.myAttempts(moduleId),
    queryFn: () => training.listMyAttempts(moduleId),
    enabled,
    retry: false,
  });
}

export function useSubmitAttempt(moduleId: string) {
  const queryClient = useQueryClient();
  return useIdempotentMutation(
    (body: AttemptSubmitRequest, key) => training.submitAttempt(moduleId, body, key),
    {
      onSuccess: () => {
        // A pass may unlock the next module, so the course list is read again.
        void queryClient.invalidateQueries({ queryKey: trainingKeys.myAttempts(moduleId) });
        void queryClient.invalidateQueries({ queryKey: trainingKeys.myCourses });
      },
    },
  );
}

// --- an administrator -------------------------------------------------------------------

export function useCourses() {
  return useQuery({ queryKey: trainingKeys.courses, queryFn: training.listCourses });
}

export function useCourse(courseId: string) {
  return useQuery({
    queryKey: trainingKeys.course(courseId),
    queryFn: () => training.getCourse(courseId),
  });
}

export function useModules(courseId: string) {
  return useQuery({
    queryKey: trainingKeys.modules(courseId),
    queryFn: () => training.listModules(courseId),
  });
}

export function useAdminAssessment(moduleId: string, enabled: boolean) {
  return useQuery({
    queryKey: trainingKeys.assessment(moduleId),
    queryFn: () => training.getAdminAssessment(moduleId),
    enabled,
    retry: false,
  });
}

export function useEnrolments(courseId: string) {
  return useQuery({
    queryKey: trainingKeys.enrolments(courseId),
    queryFn: () => training.listEnrolments(courseId),
  });
}

export function useUsers() {
  return useQuery({ queryKey: trainingKeys.users, queryFn: training.listUsers });
}

export function useCreateCourse() {
  const queryClient = useQueryClient();
  return useIdempotentMutation(
    (body: { title: string; description?: string | null }, key) => training.createCourse(body, key),
    {
      onSuccess: () => void queryClient.invalidateQueries({ queryKey: trainingKeys.courses }),
    },
  );
}

/** Invalidate everything about one course (its list entry, its modules and its enrolments). */
function useRefreshCourse(courseId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: trainingKeys.courses });
    void queryClient.invalidateQueries({ queryKey: trainingKeys.course(courseId) });
  };
}

export function usePublishCourse(courseId: string) {
  const refresh = useRefreshCourse(courseId);
  return useIdempotentMutation((_: void, key) => training.publishCourse(courseId, key), {
    onSuccess: refresh,
  });
}

export function useArchiveCourse(courseId: string) {
  const refresh = useRefreshCourse(courseId);
  return useIdempotentMutation((_: void, key) => training.archiveCourse(courseId, key), {
    onSuccess: refresh,
  });
}

export function usePublishModule(courseId: string) {
  const refresh = useRefreshCourse(courseId);
  return useIdempotentMutation(
    (body: ModulePublishRequest, key) => training.publishModule(courseId, body, key),
    { onSuccess: refresh },
  );
}

export function useUnpublishModule(courseId: string) {
  const refresh = useRefreshCourse(courseId);
  return useIdempotentMutation((moduleId: string, key) => training.unpublishModule(moduleId, key), {
    onSuccess: refresh,
  });
}

export function useDeleteModule(courseId: string) {
  const refresh = useRefreshCourse(courseId);
  return useIdempotentMutation((moduleId: string, key) => training.deleteModule(moduleId, key), {
    onSuccess: refresh,
  });
}

export function useReorderModules(courseId: string) {
  const refresh = useRefreshCourse(courseId);
  return useIdempotentMutation(
    (moduleIds: string[], key) => training.reorderModules(courseId, moduleIds, key),
    { onSuccess: refresh },
  );
}

export function useSaveAssessment(moduleId: string) {
  const queryClient = useQueryClient();
  return useIdempotentMutation(
    (body: AssessmentPutRequest, key) => training.putAssessment(moduleId, body, key),
    {
      onSuccess: () =>
        void queryClient.invalidateQueries({ queryKey: trainingKeys.assessment(moduleId) }),
    },
  );
}

export function usePublishAssessment(moduleId: string) {
  const queryClient = useQueryClient();
  return useIdempotentMutation((_: void, key) => training.publishAssessment(moduleId, key), {
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: trainingKeys.assessment(moduleId) }),
  });
}

export function useEnrolUser(courseId: string) {
  const queryClient = useQueryClient();
  return useIdempotentMutation((userId: string, key) => training.enrolUser(courseId, userId, key), {
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: trainingKeys.enrolments(courseId) }),
  });
}

export function useWithdrawUser(courseId: string) {
  const queryClient = useQueryClient();
  return useIdempotentMutation(
    (userId: string, key) => training.withdrawUser(courseId, userId, key),
    {
      onSuccess: () =>
        void queryClient.invalidateQueries({ queryKey: trainingKeys.enrolments(courseId) }),
    },
  );
}

export function useInviteUser() {
  const queryClient = useQueryClient();
  return useIdempotentMutation(
    (body: { email: string; role: 'employee' | 'admin'; display_name?: string | null }, key) =>
      training.inviteUser(body, key),
    { onSuccess: () => void queryClient.invalidateQueries({ queryKey: trainingKeys.users }) },
  );
}
