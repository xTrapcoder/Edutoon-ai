import type { ScriptChecks } from '@/lib/jobs';
import { Alert } from '@/components/ui/feedback';

/*
 * What the server's checks did to this script, said plainly. Two separate things:
 *
 * - A lesson built from a topic alone (no document) rests on the AI's general knowledge. It is
 *   labelled as that, here and in the video's own first sentence; nothing in it was checked.
 * - Every sentence of a document-based script is judged against the verified claims it may rest on
 *   (`narration_support`). A sentence no claim supports is rewritten once and then taken out. The
 *   judge is a language model: it catches reversed causes and invented requirements that a
 *   word-overlap check cannot, and it can still be wrong either way, so this says what it removed
 *   and how many sentences it could not judge instead of claiming the script is proven.
 */

export function SupportCheckNotice({ checks }: { checks: ScriptChecks }) {
  const { generalKnowledge, support } = checks;
  const removed = support?.removed ?? [];
  const unchecked = support?.uncheckedSentences ?? 0;

  return (
    <div className="space-y-4" data-testid="support-check-notice">
      {generalKnowledge ? (
        <Alert tone="warning" title="This lesson is based on general knowledge, not on a document">
          Nothing in this script was checked against a source: it is what the AI model already knows
          about the topic, written as cautiously as it could. The video says so in its first
          sentence. Read it with a teacher&rsquo;s eye before approving, and check anything you plan
          to rely on.
        </Alert>
      ) : null}

      {!generalKnowledge && support && (support.enabled || support.removed.length > 0) ? (
        <Alert
          tone={removed.length > 0 || unchecked > 0 ? 'info' : 'success'}
          title={
            removed.length > 0
              ? `${removed.length} ${removed.length === 1 ? 'sentence was' : 'sentences were'} removed because your document does not support ${removed.length === 1 ? 'it' : 'them'}`
              : 'Every sentence was checked against your verified claims'
          }
        >
          {support.enabled ? (
            <p>
              {support.judgedSentences} sentences were each judged against the claims verified from
              your source. This is an AI judge, stricter than a word check but not a proof: it can
              still miss an error or flag a fair sentence.
              {unchecked > 0
                ? ` ${unchecked} could not be judged and were kept; read those with care.`
                : ''}
            </p>
          ) : (
            <p>
              These were taken out because they added a figure or a name your claims do not contain.
            </p>
          )}
          {removed.length > 0 ? (
            <details className="mt-3">
              <summary className="cursor-pointer font-semibold text-ink-950">
                See what was removed
              </summary>
              <ul className="mt-2 space-y-2">
                {removed.map((entry, index) => (
                  <li key={index} className="rounded-md border border-line bg-white/70 p-3">
                    <p className="text-ink-800">&ldquo;{entry.sentence}&rdquo;</p>
                    <p className="mt-1 text-[13px] text-ink-500">
                      {entry.topic ? `${entry.topic} · ` : ''}
                      {entry.reason}
                      {entry.segmentDropped
                        ? ' · the rest of that passage was too short to keep'
                        : ''}
                    </p>
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </Alert>
      ) : null}
    </div>
  );
}
