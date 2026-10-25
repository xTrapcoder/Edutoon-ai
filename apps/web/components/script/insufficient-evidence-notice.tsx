import { ArrowLeft, Clock } from 'lucide-react';
import { Button, ButtonLink } from '@/components/ui/button';
import { Alert } from '@/components/ui/feedback';
import { formatTargetDuration, pluralise } from '@/lib/format';

/** The figures the API sends with `reason: "insufficient_evidence"` (all of them, or none). */
export interface EvidenceShortfall {
  targetMs: number;
  defensibleMs: number;
  supportedClaims: number;
  topicCount: number;
}

/** Read the refusal's figures; `null` if the API did not send every one of them. */
export function readEvidenceShortfall(figures: Record<string, number>): EvidenceShortfall | null {
  const targetMs = figures.target_duration_ms;
  const defensibleMs = figures.defensible_duration_ms;
  const supportedClaims = figures.supported_claims;
  const topicCount = figures.topic_count;
  if (
    targetMs === undefined ||
    defensibleMs === undefined ||
    supportedClaims === undefined ||
    topicCount === undefined
  ) {
    return null;
  }
  return { targetMs, defensibleMs, supportedClaims, topicCount };
}

/** The shortest target a project can have (see the settings form). */
const MIN_TARGET_MS = 60_000;

/**
 * Why a script was not written: the verified claims cannot honestly fill the length asked for.
 * It states the figures and the two real remedies - accept more topics that your source
 * supports, or choose a length the evidence can carry. It offers no way to write the script
 * anyway, because stretching a few claims would mean repeating them or inventing facts.
 */
export function InsufficientEvidenceNotice({
  shortfall,
  projectId,
  onChangeLength,
}: {
  shortfall: EvidenceShortfall;
  projectId: string;
  onChangeLength: () => void;
}) {
  const { targetMs, defensibleMs, supportedClaims, topicCount } = shortfall;
  const canShorten = defensibleMs >= MIN_TARGET_MS;
  const defensibleSeconds = Math.floor(defensibleMs / 1000);
  const suggested = formatTargetDuration(defensibleSeconds * 1000);
  return (
    <Alert tone="warning">
      <p className="font-semibold">
        Your source does not yet support a {formatTargetDuration(targetMs)} video
      </p>
      <p className="mt-1">
        {pluralise(supportedClaims, 'claim')} checked against your source across{' '}
        {pluralise(topicCount, 'accepted topic')} can carry about{' '}
        {formatTargetDuration(Math.max(0, defensibleSeconds) * 1000)} of narration. Stretching them
        to {formatTargetDuration(targetMs)} would mean repeating them or inventing facts, so nothing
        was written.
      </p>
      <p className="mt-2">
        {canShorten
          ? `You can accept more topics that your source supports, or choose ${suggested} or shorter.`
          : 'Accept more topics that your source supports, or add more source material - this is too little for even the shortest video.'}
      </p>
      <div className="mt-3 flex flex-wrap gap-3">
        <ButtonLink
          href={`/projects/${projectId}/topics`}
          variant="secondary"
          leadingIcon={<ArrowLeft className="size-4" aria-hidden />}
        >
          Review topics
        </ButtonLink>
        {canShorten ? (
          <Button
            variant="secondary"
            onClick={onChangeLength}
            leadingIcon={<Clock className="size-4" aria-hidden />}
          >
            Change the length
          </Button>
        ) : null}
      </div>
    </Alert>
  );
}
