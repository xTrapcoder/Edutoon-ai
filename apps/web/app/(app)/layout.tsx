import type { ReactNode } from 'react';
import { AppShell } from '@/components/app-shell/app-shell';

// Everything in this group needs a signed-in user; `middleware.ts` enforces that.
export default function AppLayout({ children }: { children: ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
