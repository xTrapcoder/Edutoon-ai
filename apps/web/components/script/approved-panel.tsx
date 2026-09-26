import { CheckCircle2, Clapperboard, FolderOpen, Mic, Plus, Video } from 'lucide-react';
import { ButtonLink } from '@/components/ui/button';
import { Card, CardBody } from '@/components/ui/card';

const UPCOMING = [
  {
    icon: Clapperboard,
    title: 'Visual scenes',
    body: 'Each section of the script is turned into scenes to show on screen.',
  },
  {
    icon: Mic,
    title: 'Narration',
    body: 'The script is read aloud to accompany the scenes.',
  },
  {
    icon: Video,
    title: 'Rendering',
    body: 'Scenes and narration are put together into the finished video.',
  },
] as const;

/**
 * Shown once a script is approved: what was done, and - without pretending anything else works
 * yet - what the next production stages will be.
 */
export function ApprovedPanel({ projectId }: { projectId: string }) {
  return (
    <div className="space-y-6">
      <Card className="overflow-hidden border-success-100">
        <CardBody className="flex flex-col items-center gap-3 bg-success-50/50 py-10 text-center">
          <span className="flex size-16 items-center justify-center rounded-full bg-success-100 text-success-600">
            <CheckCircle2 className="size-9" aria-hidden />
          </span>
          <h2 className="text-2xl font-bold text-ink-950">Script approved</h2>
          <p className="max-w-md text-[15px] text-ink-600">
            Your educational script has been approved. It is the version the next production stages
            will be built from.
          </p>
        </CardBody>
      </Card>

      <Card>
        <CardBody className="space-y-5">
          <div>
            <h3 className="text-lg font-semibold text-ink-950">What&apos;s next?</h3>
            <p className="mt-1 max-w-2xl text-sm text-ink-500">
              These production stages are still to come. They are not available yet, and nothing is
              waiting for you to do.
            </p>
          </div>
          <ul className="grid grid-cols-1 gap-3 md:grid-cols-3">
            {UPCOMING.map((stage) => (
              <li
                key={stage.title}
                className="rounded-lg border border-dashed border-line-strong bg-surface-sunken p-4"
              >
                <div className="flex items-center gap-2.5">
                  <span className="flex size-8 items-center justify-center rounded-md bg-canvas text-ink-500">
                    <stage.icon className="size-4" aria-hidden />
                  </span>
                  <p className="text-sm font-semibold text-ink-800">{stage.title}</p>
                  <span className="ml-auto rounded-full bg-canvas px-2 py-0.5 text-[11px] font-semibold text-ink-500">
                    Coming soon
                  </span>
                </div>
                <p className="mt-2 text-[13px] leading-5 text-ink-500">{stage.body}</p>
              </li>
            ))}
          </ul>
          <div className="flex flex-wrap gap-3 border-t border-line pt-5">
            <ButtonLink
              href={`/projects/${projectId}`}
              variant="secondary"
              leadingIcon={<FolderOpen className="size-4" aria-hidden />}
            >
              View project
            </ButtonLink>
            <ButtonLink href="/projects/new" leadingIcon={<Plus className="size-4" aria-hidden />}>
              Create another project
            </ButtonLink>
          </div>
        </CardBody>
      </Card>
    </div>
  );
}
