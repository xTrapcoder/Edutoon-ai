import { ImageOff, Shapes } from 'lucide-react';

/**
 * How the storyboard shows an illustrated scene's visual.
 *
 * Since Phase 11 the video draws a scene's `illustration.visual` itself (a process flow, a
 * cycle, a hierarchy, a comparison or labelled parts - see the API's `domain.visual`), so the
 * panel summarises it in words instead of pretending it is a picture. A scene with no visual is
 * drawn as text; the panel says so plainly rather than promising artwork that will never come.
 *
 * `scene.content` is loosely typed (validated server-side), so nothing here trusts a shape: a
 * field of the wrong type is skipped, and a visual that cannot be summarised is shown as text.
 */

type Loose = Record<string, unknown>;

function asRecord(value: unknown): Loose | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Loose) : undefined;
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined;
}

function asItems(value: unknown): Loose[] {
  return Array.isArray(value)
    ? value.map(asRecord).filter((item): item is Loose => item !== undefined)
    : [];
}

const VISUAL_LABEL: Record<string, string> = {
  process_flow: 'Process flow',
  cycle: 'Cycle',
  hierarchy: 'Hierarchy',
  comparison: 'Comparison',
  labelled_components: 'Labelled parts',
};

/** The visual a scene carries, when it is one the summary understands. */
export function visualOf(content: unknown): Loose | undefined {
  const visual = asRecord(asRecord(asRecord(content)?.illustration)?.visual);
  const type = asString(visual?.type);
  return visual && type !== undefined && type in VISUAL_LABEL ? visual : undefined;
}

/** True when the API dropped this scene's visual because its specification was invalid. */
export function visualWasDropped(content: unknown): boolean {
  return asRecord(asRecord(content)?.illustration)?.visual_dropped === true;
}

/** A hierarchy node's depth, following `parent` indices; bounded so a bad cycle cannot loop. */
function depthOf(nodes: Loose[], index: number): number {
  let depth = 0;
  let current = nodes[index]?.parent;
  while (typeof current === 'number' && depth < 8) {
    depth += 1;
    current = nodes[current]?.parent;
  }
  return depth;
}

function Steps({ nodes, loop }: { nodes: Loose[]; loop: boolean }) {
  return (
    <ol className="list-decimal space-y-0.5 pl-5 text-ink-800">
      {nodes.map((node, index) => {
        const label = asString(node.label);
        if (!label) return null;
        const detail = asString(node.detail);
        return (
          <li key={index} className="break-words">
            {label}
            {detail ? <span className="text-ink-500"> — {detail}</span> : null}
          </li>
        );
      })}
      {loop ? <li className="list-none text-ink-500">…then back to the first step</li> : null}
    </ol>
  );
}

function Outline({ nodes }: { nodes: Loose[] }) {
  return (
    <ul className="space-y-0.5 text-ink-800">
      {nodes.map((node, index) => {
        const label = asString(node.label);
        if (!label) return null;
        return (
          <li
            key={index}
            className="break-words"
            style={{ paddingLeft: `${depthOf(nodes, index) * 16}px` }}
          >
            {label}
          </li>
        );
      })}
    </ul>
  );
}

function Columns({ columns }: { columns: Loose[] }) {
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      {columns.map((column, index) => (
        <div key={index} className="min-w-0 rounded-md bg-surface-sunken px-2 py-1.5">
          {asString(column.heading) ? (
            <p className="break-words font-medium text-ink-900">{asString(column.heading)}</p>
          ) : null}
          <ul className="list-disc space-y-0.5 pl-4 text-ink-800">
            {(Array.isArray(column.points) ? column.points : []).map((point, at) =>
              asString(point) ? (
                <li key={at} className="break-words">
                  {asString(point)}
                </li>
              ) : null,
            )}
          </ul>
        </div>
      ))}
    </div>
  );
}

function VisualBody({ visual }: { visual: Loose }) {
  const type = asString(visual.type);
  const nodes = asItems(visual.nodes);
  switch (type) {
    case 'process_flow':
      return <Steps nodes={nodes} loop={false} />;
    case 'cycle':
      return <Steps nodes={nodes} loop />;
    case 'hierarchy':
      return <Outline nodes={nodes} />;
    case 'comparison':
      return <Columns columns={asItems(visual.columns)} />;
    case 'labelled_components': {
      const parts = asItems(visual.parts);
      return (
        <div className="space-y-1">
          {asString(visual.subject) ? (
            <p className="break-words font-medium text-ink-900">{asString(visual.subject)}</p>
          ) : null}
          <Steps nodes={parts} loop={false} />
        </div>
      );
    }
    default:
      return null;
  }
}

/** A scene's visual in words - or, honestly, the fact that the scene is drawn as text. */
export function IllustrationSummary({
  content,
  description,
}: {
  content: unknown;
  description?: string;
}) {
  const visual = visualOf(content);
  if (visual) {
    const title = asString(visual.title);
    return (
      <div className="space-y-1.5 rounded-md border border-line bg-surface-sunken/60 px-2.5 py-2 text-[13px]">
        <p className="flex items-center gap-1.5 font-medium text-ink-700">
          <Shapes className="size-4 shrink-0 text-brand-600" aria-hidden />
          <span>{VISUAL_LABEL[asString(visual.type) ?? ''] ?? 'Visual'}</span>
          {title ? <span className="break-words font-normal text-ink-600">— {title}</span> : null}
        </p>
        <VisualBody visual={visual} />
      </div>
    );
  }
  const dropped = visualWasDropped(content);
  return (
    <div className="flex items-start gap-2 rounded-md border border-dashed border-line-strong bg-surface-sunken px-2.5 py-2 text-[13px] text-ink-600">
      <ImageOff className="mt-0.5 size-4 shrink-0 text-ink-400" aria-hidden />
      <span className="break-words">
        <span className="font-medium text-ink-700">
          {dropped ? 'Shown as text — its visual was invalid' : 'Shown as text — no visual'}
        </span>
        {description ? ` — ${description}` : ''}
      </span>
    </div>
  );
}
