'use client';

import { UserButton, useUser } from '@clerk/nextjs';
import {
  FolderKanban,
  GraduationCap,
  LayoutDashboard,
  Menu,
  Plus,
  Search,
  X,
  type LucideIcon,
} from 'lucide-react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { Logo } from '@/components/brand/logo';
import { useMe } from '@/lib/api/training-queries';
import { cn } from '@/lib/cn';

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  isActive: (pathname: string) => boolean;
}

/** What an employee sees: their own learning, nothing else. */
const EMPLOYEE_NAV_ITEMS: NavItem[] = [
  {
    href: '/learn',
    label: 'My learning',
    icon: GraduationCap,
    isActive: (pathname) => pathname.startsWith('/learn'),
  },
];

const NAV_ITEMS: NavItem[] = [
  {
    href: '/dashboard',
    label: 'Dashboard',
    icon: LayoutDashboard,
    isActive: (pathname) => pathname === '/dashboard',
  },
  {
    href: '/projects',
    label: 'Projects',
    icon: FolderKanban,
    isActive: (pathname) => pathname.startsWith('/projects') && pathname !== '/projects/new',
  },
  {
    href: '/projects/new',
    label: 'Create project',
    icon: Plus,
    isActive: (pathname) => pathname === '/projects/new',
  },
  {
    href: '/admin/training',
    label: 'Training',
    icon: GraduationCap,
    isActive: (pathname) => pathname.startsWith('/admin/training'),
  },
];

function NavLinks({ pathname }: { pathname: string }) {
  const me = useMe();
  // Until the API says who this is, show nothing rather than links that may not be theirs.
  const items = me.data ? (me.data.role === 'admin' ? NAV_ITEMS : EMPLOYEE_NAV_ITEMS) : [];
  return (
    <nav aria-label="Main" className="flex flex-col gap-1">
      {items.map((item) => {
        const active = item.isActive(pathname);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? 'page' : undefined}
            className={cn(
              'flex h-10 items-center gap-3 rounded-md px-3 text-sm font-medium transition-colors',
              active
                ? 'bg-brand-50 font-semibold text-brand-700'
                : 'text-ink-600 hover:bg-canvas hover:text-ink-950',
            )}
          >
            <item.icon
              className={cn('size-[18px]', active ? 'text-brand-600' : 'text-ink-400')}
              aria-hidden
            />
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

function AccountBlock() {
  const { user } = useUser();
  const email = user?.primaryEmailAddress?.emailAddress;
  return (
    <div className="flex items-center gap-3 rounded-lg border border-line bg-surface-sunken p-3">
      <UserButton />
      <div className="min-w-0">
        <p className="truncate text-sm font-semibold text-ink-900">
          {user?.fullName ?? 'Your account'}
        </p>
        {email ? <p className="truncate text-xs text-ink-500">{email}</p> : null}
      </div>
    </div>
  );
}

function SidebarContent({ pathname, onNavigate }: { pathname: string; onNavigate?: () => void }) {
  return (
    <div
      className="flex h-full flex-col gap-8 p-5"
      onClickCapture={(event) => {
        if ((event.target as HTMLElement).closest('a')) onNavigate?.();
      }}
    >
      <Logo href="/dashboard" className="px-1" />
      <NavLinks pathname={pathname} />
      <div className="mt-auto">
        <AccountBlock />
      </div>
    </div>
  );
}

function MobileNav({
  open,
  onClose,
  pathname,
}: {
  open: boolean;
  onClose: () => void;
  pathname: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      aria-label="Menu"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === ref.current) onClose();
      }}
      className="m-0 h-full max-h-none w-72 max-w-[85vw] rounded-none bg-surface p-0 shadow-pop backdrop:bg-ink-950/45 open:animate-fade-in"
    >
      {open ? (
        <div className="relative h-full">
          <button
            type="button"
            onClick={onClose}
            aria-label="Close menu"
            className="absolute top-4 right-3 rounded-md p-2 text-ink-500 hover:bg-canvas hover:text-ink-900"
          >
            <X className="size-5" aria-hidden />
          </button>
          <SidebarContent pathname={pathname} onNavigate={onClose} />
        </div>
      ) : null}
    </dialog>
  );
}

function Topbar({ onMenu }: { onMenu: () => void }) {
  const router = useRouter();
  const [query, setQuery] = useState('');

  const search = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = query.trim();
    router.push(trimmed ? `/projects?q=${encodeURIComponent(trimmed)}` : '/projects');
  };

  return (
    <header className="sticky top-0 z-30 flex h-16 items-center gap-3 border-b border-line bg-surface/90 px-4 backdrop-blur sm:px-6 lg:px-10">
      <button
        type="button"
        onClick={onMenu}
        aria-label="Open menu"
        className="-ml-2 rounded-md p-2 text-ink-600 hover:bg-canvas lg:hidden"
      >
        <Menu className="size-5" aria-hidden />
      </button>
      <Logo href="/dashboard" className="lg:hidden" />
      <form
        role="search"
        onSubmit={search}
        className="ml-auto hidden w-full max-w-md sm:block lg:ml-0"
      >
        <label htmlFor="global-search" className="sr-only">
          Search projects
        </label>
        <div className="relative">
          <Search
            className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-ink-400"
            aria-hidden
          />
          <input
            id="global-search"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search projects"
            className="h-10 w-full rounded-md border border-line bg-canvas pr-3 pl-9 text-sm text-ink-900 placeholder:text-ink-500 hover:border-line-strong focus:border-brand-500 focus:bg-surface focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-brand-500/40"
          />
        </div>
      </form>
      <div className="ml-auto flex items-center gap-3 lg:hidden">
        <UserButton />
      </div>
    </header>
  );
}

/**
 * Sends each person to the part of the product that is theirs: an employee only ever sees
 * `/learn`, an administrator never lands there. The API enforces the same split on every route
 * (an employee gets a 404 for a project), so this is only about not showing a dead end.
 */
function useRoleRedirect(pathname: string) {
  const router = useRouter();
  const me = useMe();
  const role = me.data?.role;
  useEffect(() => {
    if (role === 'employee' && !pathname.startsWith('/learn')) router.replace('/learn');
    if (role === 'admin' && pathname.startsWith('/learn')) router.replace('/dashboard');
  }, [role, pathname, router]);
}

/** UI QA mode only: act as the administrator or as the employee, to see both sides. */
function QaPersonaSwitch() {
  const me = useMe();
  const role = me.data?.role;
  if (!role) return null;
  const other = role === 'admin' ? 'employee' : 'admin';
  return (
    <button
      type="button"
      className="ml-3 underline"
      onClick={() => {
        try {
          window.localStorage.setItem('edutoon-qa-persona', other);
        } catch {
          // Without storage the switch cannot persist; the page stays as it is.
        }
        window.location.href = other === 'employee' ? '/learn' : '/dashboard';
      }}
    >
      Acting as {role === 'admin' ? 'administrator' : 'employee'} - switch to {other}
    </button>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);
  useRoleRedirect(pathname);

  useEffect(() => {
    setMenuOpen(false);
  }, [pathname]);

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[256px_minmax(0,1fr)]">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-brand-700 px-4 py-2 text-sm font-semibold text-white focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        Skip to main content
      </a>
      <aside className="sticky top-0 hidden h-screen border-r border-line bg-surface lg:block">
        <SidebarContent pathname={pathname} />
      </aside>
      <MobileNav open={menuOpen} onClose={() => setMenuOpen(false)} pathname={pathname} />
      <div className="flex min-w-0 flex-col">
        {process.env.NEXT_PUBLIC_EDUTOON_UI_QA === '1' ? (
          <p className="bg-warning-100 px-4 py-1.5 text-center text-xs font-semibold text-warning-700">
            UI QA mode: stand-in sign-in, test data only (QA database or mock). Never enabled in a
            build.
            <QaPersonaSwitch />
          </p>
        ) : null}
        <Topbar onMenu={() => setMenuOpen(true)} />
        <main
          id="main"
          tabIndex={-1}
          className="mx-auto w-full max-w-[1200px] flex-1 px-4 py-6 focus:outline-none sm:px-6 lg:px-10 lg:py-8"
        >
          {children}
        </main>
      </div>
    </div>
  );
}
