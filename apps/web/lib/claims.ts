import type { ClaimVerification, VerdictStatus } from '@edutoon/contracts';
import type { Tone } from '@/lib/workflow';

/** Label and tone for each verdict a claim verification can carry. */
export const CLAIM_STATUS_META: Record<VerdictStatus, { label: string; tone: Tone }> = {
  supported: { label: 'Supported', tone: 'success' },
  partially_supported: { label: 'Partially supported', tone: 'warning' },
  unsupported: { label: 'Unsupported', tone: 'neutral' },
  contradicted: { label: 'Contradicted', tone: 'danger' },
  general_knowledge: { label: 'General knowledge, not checked', tone: 'warning' },
};

export interface ClaimStatusCounts {
  total: number;
  supported: number;
  partiallySupported: number;
  unsupported: number;
  contradicted: number;
  /** The AI's own knowledge in a topic-only lesson: never checked against a source. */
  generalKnowledge: number;
}

/** How many claims fall into each verdict, out of the total given. */
export function summariseClaimStatuses(claims: readonly ClaimVerification[]): ClaimStatusCounts {
  return {
    total: claims.length,
    supported: claims.filter((claim) => claim.status === 'supported').length,
    partiallySupported: claims.filter((claim) => claim.status === 'partially_supported').length,
    unsupported: claims.filter((claim) => claim.status === 'unsupported').length,
    contradicted: claims.filter((claim) => claim.status === 'contradicted').length,
    generalKnowledge: claims.filter((claim) => claim.status === 'general_knowledge').length,
  };
}

/** The claims extracted for one topic - never claims typed in by hand (`topic_id` is `null` for those). */
export function claimsForTopic(
  claims: readonly ClaimVerification[],
  topicId: string,
): ClaimVerification[] {
  return claims.filter((claim) => claim.topic_id === topicId);
}
