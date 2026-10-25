import type { ScriptDetail, Topic } from '@edutoon/contracts';
import { Alert } from '@/components/ui/feedback';
import type { WeakCitation } from '@/lib/jobs';
import { segmentTitle } from './script-model';

/*
 * An ADVISORY, never a gate. The flag comes from a rough word-overlap heuristic on the server
 * (`weak_citation_segments`, domain.script_provenance.weak_citation_coverage): it asks how much of
 * a passage's wording appears in the claims the passage cites, and says nothing about whether the
 * passage is true or whether the citation supports it.
 *
 * Observed so far, in one run: 5 of 35 passages were flagged and exactly one of them was a
 * confirmed mismatch (a "Direct investments" passage citing a regulators claim). That is a high
 * false-positive rate - analogies, examples and introductions legitimately use words the claim
 * does not. It must NOT become a blocking rule, or approval must not depend on it, without a better
 * validation than this heuristic has had.
 */

const EXCERPT_CHARS = 180;

function excerpt(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length <= EXCERPT_CHARS ? flat : `${flat.slice(0, EXCERPT_CHARS).trimEnd()}...`;
}

export function WeakCitationNotice({
  script,
  topics,
  weak,
}: {
  script: ScriptDetail;
  topics: readonly Topic[];
  weak: readonly WeakCitation[];
}) {
  const flagged = weak
    .map((entry) => ({
      entry,
      segment: script.segments.find((candidate) => candidate.segment_index === entry.segmentIndex),
    }))
    .filter(
      (item): item is typeof item & { segment: NonNullable<typeof item.segment> } =>
        item.segment !== undefined,
    );
  if (flagged.length === 0) return null;

  return (
    <Alert
      tone="warning"
      title={`Check ${flagged.length} ${flagged.length === 1 ? 'citation' : 'citations'} before approving (advisory)`}
      data-testid="weak-citation-notice"
    >
      <p>
        An automatic word-overlap check found few of these passages&rsquo; words in the claims they
        cite. It is a rough guide, not proof either way, and it often flags fair examples and
        analogies. It does not stop you approving. Read each passage against its cited claims.
      </p>
      <ul className="mt-3 space-y-3">
        {flagged.map(({ entry, segment }) => (
          <li
            key={segment.id}
            data-segment-id={segment.id}
            className="rounded-md border border-warning-100 bg-white/70 p-3"
          >
            <p className="font-medium text-ink-950">
              Passage {segment.segment_index + 1} &middot; {segmentTitle(segment, topics)}
              <span className="ml-2 font-normal text-ink-500">
                about {Math.round(entry.coverage * 100)}% of its words are in the cited claims
              </span>
            </p>
            <p className="mt-1 text-ink-700">&ldquo;{excerpt(segment.narration_text)}&rdquo;</p>
            <p className="mt-2 text-xs font-semibold uppercase tracking-wide text-ink-500">
              Cited {segment.claims.length === 1 ? 'claim' : 'claims'}
            </p>
            {segment.claims.length === 0 ? (
              <p className="text-ink-500">None.</p>
            ) : (
              <ul className="list-disc pl-5 text-ink-700">
                {segment.claims.map((claim) => (
                  <li key={claim.id}>{claim.claim_text}</li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </Alert>
  );
}
