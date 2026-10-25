import { CheckCircle2, FolderOpen, Plus, Video } from 'lucide-react';
import { ButtonLink } from '@/components/ui/button';
import { Card, CardBody } from '@/components/ui/card';

/**
 * Shown once a script is approved: what was done, and the real stages that follow. Narration
 * (Phase 8), the storyboard (Phase 9) and the video (Phase 10) all exist; each is offered directly
 * below, in Script Studio's own panels, and the video only once its real prerequisites are met.
 * "Go to Video" links to the Video page (`/video`), it does not start a render.
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
            <p className="mt-1 text-sm font-semibold text-ink-800">
              Next: Narration → Storyboard → Video
            </p>
            <p className="mt-1 max-w-2xl text-sm text-ink-500">
              Each stage is offered in Script Studio below, in that order. The video can be rendered
              once the narration is complete and the storyboard is finished.
            </p>
          </div>
          <div className="flex flex-wrap gap-3 border-t border-line pt-5">
            <ButtonLink
              href={`/projects/${projectId}/video`}
              leadingIcon={<Video className="size-4" aria-hidden />}
            >
              Go to Video
            </ButtonLink>
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
