import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { navigation, router } from '@/test/navigation';
import { renderWithClient } from '@/test/render';
import { API, server } from '@/test/server';
import { AppShell } from './app-shell';

const me = (role: 'admin' | 'employee') =>
  http.get(`${API}/v1/auth/me`, () =>
    HttpResponse.json({ id: 'u1', email: 'a@example.test', display_name: null, role }),
  );

function labels() {
  // The sidebar is rendered once for desktop and once inside the (closed) mobile menu; either one.
  const [nav] = screen.getAllByRole('navigation', { name: 'Main' });
  return within(nav!)
    .queryAllByRole('link')
    .map((link) => link.textContent);
}

describe('The app shell by role', () => {
  it('gives an administrator the projects and the training screens', async () => {
    server.use(me('admin'));
    navigation.pathname = '/dashboard';
    renderWithClient(<AppShell>page</AppShell>);

    await waitFor(() => expect(labels()).toContain('Training'), { timeout: 5000 });
    expect(labels()).toEqual(['Dashboard', 'Projects', 'Create project', 'Training']);
    expect(router.replace).not.toHaveBeenCalled();
  });

  it('gives an employee only their own learning, and sends them there from anywhere else', async () => {
    server.use(me('employee'));
    navigation.pathname = '/projects';
    renderWithClient(<AppShell>page</AppShell>);

    await waitFor(() => expect(labels()).toEqual(['My learning']), { timeout: 5000 });
    expect(router.replace).toHaveBeenCalledWith('/learn');
  });

  it('leaves an employee alone on their own screens', async () => {
    server.use(me('employee'));
    navigation.pathname = '/learn/some-module';
    renderWithClient(<AppShell>page</AppShell>);

    await waitFor(() => expect(labels()).toEqual(['My learning']), { timeout: 5000 });
    expect(router.replace).not.toHaveBeenCalled();
  });

  it('keeps an administrator out of the learner screens', async () => {
    server.use(me('admin'));
    navigation.pathname = '/learn';
    renderWithClient(<AppShell>page</AppShell>);

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith('/dashboard'), {
      timeout: 5000,
    });
  });

  it('shows no links at all until the API has said who the person is', () => {
    server.use(http.get(`${API}/v1/auth/me`, () => new Promise(() => undefined)));
    navigation.pathname = '/dashboard';
    renderWithClient(<AppShell>page</AppShell>);

    expect(labels()).toEqual([]);
  });
});
