import { vi } from 'vitest';

/** What `next/navigation` reports in tests; set it per test, and read `router` to see where the app went. */
export const navigation = {
  pathname: '/',
  projectId: '',
  search: '',
};

export const router = {
  push: vi.fn(),
  replace: vi.fn(),
  back: vi.fn(),
  refresh: vi.fn(),
  prefetch: vi.fn(),
};

export function resetNavigation() {
  navigation.pathname = '/';
  navigation.projectId = '';
  navigation.search = '';
  Object.values(router).forEach((fn) => fn.mockReset());
}
