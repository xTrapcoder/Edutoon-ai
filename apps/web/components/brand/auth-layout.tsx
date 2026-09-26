import type { ReactNode } from 'react';
import { Logo } from './logo';

/** The frame around Clerk's sign-in and sign-up screens. */
export function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <main
      id="main"
      className="flex min-h-screen flex-col items-center justify-center gap-8 bg-[radial-gradient(60%_50%_at_50%_0%,var(--color-brand-100),transparent)] px-4 py-12"
    >
      <Logo />
      {children}
      <p className="max-w-sm text-center text-[13px] text-ink-500">
        Every video traceable to verified source material.
      </p>
    </main>
  );
}
