import {
  ArrowDown,
  Clapperboard,
  Clock,
  FileText,
  FileVideo,
  Film,
  FolderPlus,
  Gauge,
  Languages,
  ListTree,
  MonitorPlay,
  PenLine,
  ShieldCheck,
  Smartphone,
  Video,
  Volume2,
} from 'lucide-react';
import type { Metadata } from 'next';
import { Logo } from '@/components/brand/logo';
import { Badge } from '@/components/ui/badge';
import { ButtonLink } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Alert } from '@/components/ui/feedback';
import { cn } from '@/lib/cn';
import { DEMO_VIDEOS, demoVideoSize, type DemoVideo } from '@/lib/demo-video';
import { formatBytes } from '@/lib/format';

export const metadata: Metadata = { title: 'Video Demo' };

// Read the disk on every request, so the page reflects whether the verified renders are present.
export const dynamic = 'force-dynamic';

const PIPELINE = [
  { icon: FileText, title: 'PDF', body: 'Your study material, up to 100 MB.' },
  {
    icon: ListTree,
    title: 'AI Topic Extraction',
    body: 'Finds the topics worth covering, with the pages each one comes from.',
  },
  {
    icon: ShieldCheck,
    title: 'Evidence Verification',
    body: 'Every claim is checked against your source before a script can use it.',
  },
  {
    icon: PenLine,
    title: 'Educational Script',
    body: 'Written only from verified claims. You review and approve it.',
  },
  {
    icon: Volume2,
    title: 'Narration',
    body: 'English (India) setting, spoken by a local, offline voice - a British-English stand-in voice until a genuine Indian-English one is available.',
  },
  {
    icon: Clapperboard,
    title: 'Storyboard & Visual Scenes',
    body: 'A timed, templated scene timeline built from the narration.',
  },
  { icon: Video, title: '60 FPS Video', body: 'Rendered to an MP4 you can play and download.' },
] as const;

const SETTINGS = [
  { icon: MonitorPlay, label: 'Horizontal 16:9' },
  { icon: Smartphone, label: 'Vertical 9:16' },
  { icon: Clock, label: 'Duration: 1–60 minutes' },
  { icon: Languages, label: 'English (India)' },
  { icon: Gauge, label: '60 FPS' },
  { icon: FileVideo, label: 'H.264 / AAC' },
] as const;

function DemoPlayer({ video }: { video: DemoVideo }) {
  const size = demoVideoSize(video.key);
  const vertical = video.key === 'vertical';

  return (
    <figure className="space-y-3">
      <figcaption className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-ink-950">{video.title}</span>
        <Badge tone="brand">{video.ratio}</Badge>
      </figcaption>
      {size === null ? (
        <Alert tone="warning" title="This render is not on this machine">
          {video.file} is missing from apps/api/var/phase11-verification/. It is created by
          apps/api/scripts/verify_phase11.py.
        </Alert>
      ) : (
        <video
          controls
          preload="metadata"
          src={`/demo-video/${video.key}`}
          aria-label={`${video.title}, ${video.ratio}`}
          className={cn(
            'w-full rounded-md bg-ink-950',
            vertical ? 'mx-auto aspect-[9/16] max-h-[70vh] max-w-[280px]' : 'aspect-video',
          )}
        >
          Your browser does not support the video element.
        </video>
      )}
      <p className="text-[13px] text-ink-500">
        {video.frame} · 60 fps · H.264 + AAC · {video.duration}
        {size !== null ? ` · ${formatBytes(size)}` : ''}
      </p>
      <p className="text-xs break-all text-ink-400">File: {video.file}</p>
    </figure>
  );
}

export default function VideoDemoPage() {
  return (
    <div className="bg-surface">
      <header className="sticky top-0 z-30 border-b border-line bg-surface/90 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-6xl items-center gap-4 px-4 sm:px-6">
          <Logo />
          <div className="ml-auto flex items-center gap-3">
            <ButtonLink href="/dashboard" variant="secondary" size="sm">
              Dashboard
            </ButtonLink>
            <ButtonLink
              href="/projects/new"
              size="sm"
              leadingIcon={<FolderPlus className="size-4" aria-hidden />}
            >
              Create Project
            </ButtonLink>
          </div>
        </div>
      </header>

      <main id="main">
        <section className="bg-[radial-gradient(60%_50%_at_75%_10%,var(--color-brand-50),transparent)]">
          <div className="mx-auto max-w-6xl px-4 py-12 sm:px-6 lg:py-16">
            <p className="inline-flex items-center gap-2 rounded-full bg-brand-50 px-3 py-1 text-[13px] font-semibold text-brand-700">
              <Film className="size-4" aria-hidden />
              EduToon AI
            </p>
            <h1 className="mt-4 text-4xl font-bold tracking-tight text-ink-950 sm:text-5xl">
              AI Educational Video Generator
            </h1>
            <p className="mt-4 max-w-2xl text-lg leading-8 text-ink-600">
              Turn a PDF or other educational content into a verified, narrated, animated explainer
              video - every claim traced back to your source.
            </p>
          </div>
        </section>

        <div className="mx-auto max-w-6xl space-y-8 px-4 pb-16 sm:px-6">
          <Card>
            <CardHeader
              title="Verified Local Render Demo"
              description="Two real MP4 files rendered by EduToon AI's local rendering pipeline, one for each video shape."
              icon={<Film className="size-4" aria-hidden />}
              action={
                <Badge tone="success" dot>
                  Locally verified
                </Badge>
              }
            />
            <CardBody className="space-y-6">
              <div className="grid grid-cols-1 gap-8 lg:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)]">
                <DemoPlayer video={DEMO_VIDEOS.horizontal} />
                <DemoPlayer video={DEMO_VIDEOS.vertical} />
              </div>
              <Alert title="What you are watching" className="space-y-1">
                <p>
                  This is a locally verified render of the existing EduToon AI rendering pipeline.
                  Live AI generation requires the configured provider credentials.
                </p>
                <p>
                  The content is a verification sample, not made from an uploaded PDF. Its narration
                  is spoken by the local offline voice and its scenes were drawn by the local
                  renderer; a test script stood in for the AI steps.
                </p>
              </Alert>
            </CardBody>
          </Card>

          <div className="grid grid-cols-1 gap-8 lg:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
            <Card>
              <CardHeader
                title="How a video is made"
                description="Input: PDF / Educational content"
                icon={<ListTree className="size-4" aria-hidden />}
              />
              <CardBody>
                <ol>
                  {PIPELINE.map((step, index) => (
                    <li key={step.title}>
                      <div className="flex items-start gap-3 rounded-lg border border-line bg-surface p-3">
                        <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-brand-50 text-brand-700">
                          <step.icon className="size-4" aria-hidden />
                        </span>
                        <div className="min-w-0">
                          <p className="text-sm font-semibold text-ink-950">{step.title}</p>
                          <p className="mt-0.5 text-[13px] text-ink-500">{step.body}</p>
                        </div>
                      </div>
                      {index < PIPELINE.length - 1 ? (
                        <ArrowDown className="mx-auto my-1.5 size-4 text-ink-400" aria-hidden />
                      ) : null}
                    </li>
                  ))}
                </ol>
              </CardBody>
            </Card>

            <div className="space-y-8">
              <Card>
                <CardHeader
                  title="What you can set"
                  description="Chosen when you create a project."
                  icon={<Gauge className="size-4" aria-hidden />}
                />
                <CardBody className="space-y-4">
                  <ul className="flex flex-wrap gap-2.5">
                    {SETTINGS.map((setting) => (
                      <li
                        key={setting.label}
                        className="inline-flex items-center gap-2 rounded-full border border-line bg-surface px-3.5 py-1.5 text-sm font-medium text-ink-700"
                      >
                        <setting.icon className="size-4 text-brand-600" aria-hidden />
                        {setting.label}
                      </li>
                    ))}
                  </ul>
                  <p className="text-[13px] text-ink-500">
                    Script lengths from 1 to 60 minutes are supported by the project model. Today
                    the video render and the project form are limited to 10:00.
                  </p>
                </CardBody>
              </Card>

              <Card>
                <CardBody className="space-y-4">
                  <div>
                    <h2 className="text-lg font-semibold text-ink-950">Try the real workflow</h2>
                    <p className="mt-1 text-sm text-ink-500">
                      Create a project to follow the real path: topics, verified claims, script,
                      approval, narration, storyboard, then video.
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-3">
                    <ButtonLink
                      href="/projects/new"
                      size="lg"
                      leadingIcon={<FolderPlus className="size-5" aria-hidden />}
                    >
                      Create Project
                    </ButtonLink>
                    <ButtonLink href="/dashboard" variant="secondary" size="lg">
                      Back to dashboard
                    </ButtonLink>
                  </div>
                </CardBody>
              </Card>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
