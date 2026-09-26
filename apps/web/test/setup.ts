import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, vi } from 'vitest';
import { navigation, resetNavigation, router } from './navigation';
import { server } from './server';

// No real Clerk in tests: the token is fixed, and nothing signs in.
vi.mock('@clerk/nextjs', () => ({
  getToken: async () => 'test-token',
  useUser: () => ({
    user: {
      firstName: 'Asha',
      fullName: 'Asha Rao',
      primaryEmailAddress: { emailAddress: 'asha@example.test' },
    },
  }),
  UserButton: () => null,
  Show: ({ children }: { children: React.ReactNode }) => children,
}));

// The app reads its route and router from next/navigation; tests set them in ./navigation.
vi.mock('next/navigation', () => ({
  useRouter: () => router,
  usePathname: () => navigation.pathname,
  useParams: () => ({ projectId: navigation.projectId }),
  useSearchParams: () => new URLSearchParams(navigation.search),
}));

// jsdom has no <dialog> behaviour: give it the two methods the app relies on.
if (!HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.removeAttribute('open');
  };
}

window.matchMedia ||= ((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
  dispatchEvent: () => false,
})) as typeof window.matchMedia;
Element.prototype.scrollIntoView ||= () => {};

// Any request a test did not describe fails loudly instead of quietly reaching a network.
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
  resetNavigation();
});
afterAll(() => server.close());
