'use client';

/**
 * UI QA stand-in for `@clerk/nextjs`. Test tooling only - see `next.config.ts`.
 *
 * This is NOT authentication. It is aliased in solely when `EDUTOON_UI_QA=1` and `next dev` is
 * running, so the signed-in screens can be looked at without a Clerk instance. It signs nobody
 * in and verifies nothing; the mock API next to it accepts any bearer token. The real
 * `@clerk/nextjs` is what ships.
 */

import { useEffect, useState, type ReactNode } from 'react';

const SIGNED_OUT_COOKIE = 'edutoon_qa_signed_out=1';

function useSignedIn(): boolean {
  const [signedIn, setSignedIn] = useState(true);
  useEffect(() => {
    setSignedIn(!document.cookie.includes(SIGNED_OUT_COOKIE));
  }, []);
  return signedIn;
}

export function ClerkProvider({ children }: { children: ReactNode; [prop: string]: unknown }) {
  return <>{children}</>;
}

export function Show({
  when,
  children,
}: {
  when: 'signed-in' | 'signed-out';
  children: ReactNode;
}) {
  const signedIn = useSignedIn();
  return <>{(when === 'signed-in') === signedIn ? children : null}</>;
}

export function useUser() {
  return {
    isLoaded: true,
    isSignedIn: true,
    user: {
      firstName: 'Priya',
      fullName: 'Priya Nair',
      primaryEmailAddress: { emailAddress: 'priya@example.test' },
    },
  };
}

export function UserButton() {
  return (
    <span
      role="img"
      aria-label="Account menu (UI QA stand-in)"
      className="flex size-8 items-center justify-center rounded-full bg-brand-600 text-xs font-semibold text-white"
    >
      PN
    </span>
  );
}

function Placeholder({ title }: { title: string }) {
  return (
    <div className="w-full max-w-sm rounded-xl border border-line bg-surface p-8 text-center shadow-raised">
      <h1 className="text-lg font-semibold text-ink-950">{title}</h1>
      <p className="mt-2 text-sm text-ink-500">
        Clerk renders this screen. UI QA mode shows a placeholder instead, because it has no Clerk
        instance.
      </p>
    </div>
  );
}

export function SignIn() {
  return <Placeholder title="Sign in" />;
}

export function SignUp() {
  return <Placeholder title="Create your account" />;
}

export async function getToken(): Promise<string | null> {
  return 'ui-qa-token';
}
