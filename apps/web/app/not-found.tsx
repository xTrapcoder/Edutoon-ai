import { FileQuestion } from 'lucide-react';
import { Logo } from '@/components/brand/logo';
import { ButtonLink } from '@/components/ui/button';

export default function NotFound() {
  return (
    <main
      id="main"
      className="flex min-h-screen flex-col items-center justify-center gap-6 px-4 text-center"
    >
      <Logo />
      <span className="flex size-14 items-center justify-center rounded-full bg-brand-50 text-brand-600">
        <FileQuestion className="size-7" aria-hidden />
      </span>
      <div>
        <h1 className="text-2xl font-bold text-ink-950">We couldn&apos;t find that page</h1>
        <p className="mt-2 max-w-md text-ink-500">
          The link may be out of date, or the page may have moved. Head back and try from there.
        </p>
      </div>
      <ButtonLink href="/dashboard">Go to your dashboard</ButtonLink>
    </main>
  );
}
