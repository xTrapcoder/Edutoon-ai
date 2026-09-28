import { Show } from '@clerk/nextjs';
import {
  ArrowRight,
  BookOpenCheck,
  CheckCircle2,
  Clapperboard,
  FileText,
  ListTree,
  PenLine,
  ShieldCheck,
  Video,
} from 'lucide-react';
import Link from 'next/link';
import { Logo } from '@/components/brand/logo';
import { ButtonLink } from '@/components/ui/button';

const HIGHLIGHTS = [
  'Works from a PDF, a list of topics, or both',
  'Topics come with the pages they were drawn from',
  'Scripts are written only from claims checked against your source',
  'Nothing moves on until you approve it',
] as const;

const FEATURES = [
  {
    icon: FileText,
    title: 'Start from your own material',
    body: 'Upload a PDF, type the topics you want to teach, or combine the two. Your source is the ground truth.',
  },
  {
    icon: ListTree,
    title: 'Topics with their sources',
    body: 'See the outline we find, with the pages each topic comes from. Accept, reject or reword every one.',
  },
  {
    icon: ShieldCheck,
    title: 'Verified before it is written',
    body: 'Every statement in a script is checked against your material first, so you can see where it came from.',
  },
  {
    icon: BookOpenCheck,
    title: 'You stay in control',
    body: 'Review the script section by section, with its sources beside it, and approve it when you are happy.',
  },
] as const;

const STEPS = [
  ['Add your source', 'A PDF, keywords, or both, with your chosen length and language.'],
  ['Review the topics', 'Check the outline and the evidence behind each topic.'],
  ['Review the script', 'Read the cited script, one section at a time.'],
  ['Approve it', 'Lock in the script that the next stages will build on.'],
] as const;

const UPCOMING = [
  { icon: Clapperboard, title: 'Visual scenes' },
  { icon: Video, title: 'Rendered video' },
] as const;

const FAQ = [
  {
    q: 'What can I upload?',
    a: 'A PDF of up to 100 MB. You can also skip the upload and just enter the topics you want covered, or use both together.',
  },
  {
    q: 'Does it make things up?',
    a: 'A script is written only from claims that have been checked against your source material. Topics suggested from keywords alone are clearly marked as not coming from a source.',
  },
  {
    q: 'Can I make a video today?',
    a: 'Not yet. Today EduToon takes you from your material to an approved, cited script, and lets you narrate it with a local, offline voice. Visual scenes and rendering are the next stages and are still being built.',
  },
  {
    q: 'Which languages are supported?',
    a: 'English (India), which is the default, and English (UK). The language sets how topics and scripts are written.',
  },
] as const;

function PipelinePreview() {
  return (
    <div aria-hidden className="relative mx-auto w-full max-w-md">
      <div className="absolute -inset-6 -z-10 rounded-[32px] bg-gradient-to-br from-brand-100 via-accent-50 to-transparent blur-2xl" />
      <div className="space-y-3">
        <div className="flex items-center gap-3 rounded-xl border border-line bg-surface p-4 shadow-raised">
          <span className="flex size-10 items-center justify-center rounded-lg bg-danger-50 text-danger-600">
            <FileText className="size-5" />
          </span>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-ink-950">Computer_Networks.pdf</p>
            <p className="text-xs text-ink-500">42 pages</p>
          </div>
          <span className="ml-auto rounded-full bg-success-50 px-2.5 py-0.5 text-xs font-semibold text-success-700">
            Ready
          </span>
        </div>
        <div className="ml-6 rounded-xl border border-line bg-surface p-4 shadow-raised">
          <p className="mb-3 text-xs font-semibold tracking-wide text-ink-500 uppercase">Topics</p>
          <ul className="space-y-2 text-sm">
            {[
              ['Network models', 'Pages 4–9'],
              ['Protocols and standards', 'Pages 12–18'],
              ['Network devices', 'Pages 21–26'],
            ].map(([title, pages]) => (
              <li key={title} className="flex items-center gap-2">
                <span className="flex size-4 items-center justify-center rounded-full bg-success-600 text-white">
                  <CheckCircle2 className="size-3" />
                </span>
                <span className="font-medium text-ink-900">{title}</span>
                <span className="ml-auto rounded-full bg-brand-50 px-2 py-0.5 text-[11px] font-medium text-brand-700">
                  {pages}
                </span>
              </li>
            ))}
          </ul>
        </div>
        <div className="ml-12 rounded-xl border border-line bg-surface p-4 shadow-raised">
          <div className="mb-2 flex items-center gap-2">
            <PenLine className="size-4 text-brand-600" />
            <p className="text-sm font-semibold text-ink-950">Script</p>
            <span className="ml-auto rounded-full bg-accent-50 px-2 py-0.5 text-[11px] font-semibold text-accent-700">
              Every claim sourced
            </span>
          </div>
          <div className="space-y-1.5">
            <div className="h-2 w-full rounded bg-line" />
            <div className="h-2 w-11/12 rounded bg-line" />
            <div className="h-2 w-2/3 rounded bg-line" />
          </div>
        </div>
      </div>
    </div>
  );
}

export default function LandingPage() {
  return (
    <div className="bg-surface">
      <header className="sticky top-0 z-30 border-b border-line bg-surface/90 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-6xl items-center gap-6 px-4 sm:px-6">
          <Logo />
          <nav
            aria-label="Sections"
            className="ml-auto hidden items-center gap-7 text-sm font-medium text-ink-600 md:flex"
          >
            <a href="#features" className="rounded hover:text-ink-950">
              Features
            </a>
            <a href="#how-it-works" className="rounded hover:text-ink-950">
              How it works
            </a>
            <a href="#faq" className="rounded hover:text-ink-950">
              FAQ
            </a>
          </nav>
          <div className="ml-auto md:ml-0">
            <Show when="signed-in">
              <ButtonLink href="/dashboard" size="sm">
                Open dashboard
              </ButtonLink>
            </Show>
            <Show when="signed-out">
              <ButtonLink href="/sign-in" size="sm">
                Sign in
              </ButtonLink>
            </Show>
          </div>
        </div>
      </header>

      <main id="main">
        <section className="overflow-hidden bg-[radial-gradient(60%_50%_at_75%_10%,var(--color-brand-50),transparent)]">
          <div className="mx-auto grid max-w-6xl items-center gap-12 px-4 py-16 sm:px-6 lg:grid-cols-[1.05fr_0.95fr] lg:py-24">
            <div>
              <p className="inline-flex items-center gap-2 rounded-full bg-brand-50 px-3 py-1 text-[13px] font-semibold text-brand-700">
                <ShieldCheck className="size-4" aria-hidden />
                Every claim traced to your source
              </p>
              <h1 className="mt-5 text-4xl leading-[1.1] font-bold tracking-tight text-ink-950 sm:text-5xl">
                Turn your learning material into a{' '}
                <span className="text-brand-600">verified, cited script</span>
              </h1>
              <p className="mt-5 max-w-xl text-lg leading-8 text-ink-600">
                Upload a PDF or enter your topics. EduToon finds what is worth covering, checks each
                claim against your source, and prepares a script for you to review and approve.
              </p>
              <ul className="mt-6 space-y-2.5">
                {HIGHLIGHTS.map((item) => (
                  <li key={item} className="flex items-start gap-2.5 text-[15px] text-ink-700">
                    <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-success-600" aria-hidden />
                    {item}
                  </li>
                ))}
              </ul>
              <div className="mt-8 flex flex-wrap items-center gap-4">
                <Show when="signed-in">
                  <ButtonLink
                    href="/dashboard"
                    size="lg"
                    trailingIcon={<ArrowRight className="size-5" aria-hidden />}
                  >
                    Open your dashboard
                  </ButtonLink>
                </Show>
                <Show when="signed-out">
                  <ButtonLink
                    href="/sign-up"
                    size="lg"
                    trailingIcon={<ArrowRight className="size-5" aria-hidden />}
                  >
                    Get started
                  </ButtonLink>
                  <Link
                    href="/sign-in"
                    className="text-sm font-semibold text-brand-700 hover:underline"
                  >
                    I already have an account
                  </Link>
                </Show>
              </div>
              <p className="mt-4 text-[13px] text-ink-500">Secure sign-in with Clerk.</p>
            </div>
            <PipelinePreview />
          </div>
        </section>

        <section id="features" className="scroll-mt-20 border-t border-line bg-canvas">
          <div className="mx-auto max-w-6xl px-4 py-16 sm:px-6">
            <h2 className="text-3xl font-bold tracking-tight text-ink-950">
              Built so you can trust what it says
            </h2>
            <p className="mt-2 max-w-2xl text-ink-600">
              Most AI tools are asked to be believed. EduToon shows its working at every step.
            </p>
            <ul className="mt-10 grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-4">
              {FEATURES.map((feature) => (
                <li
                  key={feature.title}
                  className="rounded-xl border border-line bg-surface p-6 shadow-card"
                >
                  <span className="flex size-10 items-center justify-center rounded-lg bg-brand-50 text-brand-600">
                    <feature.icon className="size-5" aria-hidden />
                  </span>
                  <h3 className="mt-4 text-base font-semibold text-ink-950">{feature.title}</h3>
                  <p className="mt-1.5 text-sm leading-6 text-ink-600">{feature.body}</p>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section id="how-it-works" className="scroll-mt-20">
          <div className="mx-auto max-w-6xl px-4 py-16 sm:px-6">
            <h2 className="text-3xl font-bold tracking-tight text-ink-950">How it works</h2>
            <ol className="mt-10 grid grid-cols-1 gap-6 md:grid-cols-4">
              {STEPS.map(([title, body], index) => (
                <li key={title}>
                  <span className="flex size-9 items-center justify-center rounded-full bg-brand-600 text-sm font-bold text-white">
                    {index + 1}
                  </span>
                  <h3 className="mt-4 text-base font-semibold text-ink-950">{title}</h3>
                  <p className="mt-1.5 text-sm leading-6 text-ink-600">{body}</p>
                </li>
              ))}
            </ol>

            <div className="mt-12 rounded-xl border border-dashed border-line-strong bg-surface-sunken p-6">
              <p className="text-sm font-semibold text-ink-950">Coming next</p>
              <p className="mt-1 text-sm text-ink-600">
                Once a script is approved, you can already narrate it with a local, offline voice.
                Turning it into a full video still needs these stages, which are being built.
              </p>
              <ul className="mt-4 flex flex-wrap gap-3">
                {UPCOMING.map((stage) => (
                  <li
                    key={stage.title}
                    className="inline-flex items-center gap-2 rounded-full border border-line bg-surface px-3.5 py-1.5 text-sm font-medium text-ink-600"
                  >
                    <stage.icon className="size-4 text-ink-400" aria-hidden />
                    {stage.title}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </section>

        <section id="faq" className="scroll-mt-20 border-t border-line bg-canvas">
          <div className="mx-auto max-w-3xl px-4 py-16 sm:px-6">
            <h2 className="text-3xl font-bold tracking-tight text-ink-950">Questions</h2>
            <div className="mt-8 divide-y divide-line rounded-xl border border-line bg-surface shadow-card">
              {FAQ.map((item) => (
                <details key={item.q} className="group p-5">
                  <summary className="flex cursor-pointer list-none items-center justify-between gap-4 text-[15px] font-semibold text-ink-950 [&::-webkit-details-marker]:hidden">
                    {item.q}
                    <span
                      aria-hidden
                      className="text-xl leading-none text-brand-600 transition-transform group-open:rotate-45"
                    >
                      +
                    </span>
                  </summary>
                  <p className="mt-3 text-sm leading-6 text-ink-600">{item.a}</p>
                </details>
              ))}
            </div>
          </div>
        </section>
      </main>

      <footer className="border-t border-line bg-surface">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4 px-4 py-8 text-sm text-ink-500 sm:px-6">
          <Logo />
          <p>Every video traceable to verified source material.</p>
        </div>
      </footer>
    </div>
  );
}
