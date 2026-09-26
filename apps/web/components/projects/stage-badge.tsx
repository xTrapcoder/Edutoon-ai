import { Badge } from '@/components/ui/badge';
import { STAGES, type StageId } from '@/lib/workflow';

/** A project's stage as a coloured badge. A pulsing dot means work is running in the background. */
export function StageBadge({ stage }: { stage: StageId }) {
  const meta = STAGES[stage];
  return (
    <Badge tone={meta.tone} dot busy={meta.busy}>
      {meta.label}
    </Badge>
  );
}
