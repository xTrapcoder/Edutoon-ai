import type { Job, Project, Script, Source, Topic } from '@edutoon/contracts';
import { isActiveJob, latestJobOfKind } from '@/lib/jobs';
import { isFileProject, sourceWords } from '@/lib/source-kind';

/**
 * Where a project is in its journey, worked out from what the API actually returns.
 *
 * The API's `projects.status` (draft / processing / ready / failed / archived) is only a coarse
 * roll-up of the pipeline's latest outcome. It cannot say *which* step a project is at - topics
 * extracted, script drafted, script approved - so the stage comes from the project's sources,
 * topics, jobs and scripts instead, so it is always what is really stored.
 * Pass what you have; a missing list is treated as "not known yet" and never as "empty".
 */

export type StageId =
  | 'unknown'
  | 'needs_source'
  | 'source_failed'
  | 'reading_source'
  | 'ready_for_topics'
  | 'extracting_topics'
  | 'topics_failed'
  | 'topics_ready'
  | 'writing_script'
  | 'script_ready'
  | 'script_failed'
  | 'approved';

export type Tone = 'neutral' | 'info' | 'brand' | 'success' | 'warning' | 'danger';

export interface StageMeta {
  label: string;
  tone: Tone;
  /** Work is running in the background: show a live indicator. */
  busy: boolean;
}

export const STAGES: Record<StageId, StageMeta> = {
  unknown: { label: 'Draft', tone: 'neutral', busy: false },
  needs_source: { label: 'Needs source', tone: 'warning', busy: false },
  source_failed: { label: 'Source problem', tone: 'danger', busy: false },
  reading_source: { label: 'Reading source', tone: 'info', busy: true },
  ready_for_topics: { label: 'Ready for topics', tone: 'neutral', busy: false },
  extracting_topics: { label: 'Extracting topics', tone: 'info', busy: true },
  topics_failed: { label: 'Extraction failed', tone: 'danger', busy: false },
  topics_ready: { label: 'Topics ready', tone: 'brand', busy: false },
  writing_script: { label: 'Writing script', tone: 'info', busy: true },
  script_ready: { label: 'Script ready', tone: 'brand', busy: false },
  script_failed: { label: 'Script failed', tone: 'danger', busy: false },
  approved: { label: 'Script approved', tone: 'success', busy: false },
};

export type StepId = 'source' | 'topics' | 'script' | 'approved';
export type StepState = 'complete' | 'current' | 'upcoming';

export interface Step {
  id: StepId;
  label: string;
  state: StepState;
  /** The current step failed and needs the person's attention. */
  attention: boolean;
  /** Whether the person can open this step's page yet. Later stages are never functional early. */
  reachable: boolean;
  detail: string | null;
}

export interface NextAction {
  label: string;
  href: string;
}

export interface Workflow {
  stage: StageId;
  meta: StageMeta;
  steps: Step[];
  next: NextAction | null;
  /** The script a person is working with, if any (the newest that has not been superseded). */
  script: Script | undefined;
  /** The PDF or deck the project is built from, if any. */
  source: Source | undefined;
  /** Topics that have not been rejected. */
  usableTopicCount: number;
  canOpenTopics: boolean;
  canOpenScript: boolean;
}

export interface WorkflowInput {
  project: Project;
  sources?: readonly Source[] | undefined;
  topics?: readonly Topic[] | undefined;
  jobs?: readonly Job[] | undefined;
  scripts?: readonly Script[] | undefined;
}

const STAGE_STEP: Record<StageId, number> = {
  unknown: 0,
  needs_source: 0,
  source_failed: 0,
  reading_source: 0,
  ready_for_topics: 1,
  extracting_topics: 1,
  topics_failed: 1,
  topics_ready: 1,
  writing_script: 2,
  script_ready: 2,
  script_failed: 2,
  approved: 3,
};

const FAILED_STAGES: ReadonlySet<StageId> = new Set([
  'source_failed',
  'topics_failed',
  'script_failed',
]);

const STEP_LABELS: Record<StepId, string> = {
  source: 'Source',
  topics: 'Topics',
  script: 'Script',
  approved: 'Approved',
};
const STEP_ORDER: StepId[] = ['source', 'topics', 'script', 'approved'];

/** The newest script that is still live. The API lists newest first; superseded ones are history. */
export function currentScript(scripts: readonly Script[] | undefined): Script | undefined {
  if (!scripts) return undefined;
  let current: Script | undefined;
  for (const script of scripts) {
    if (script.status === 'superseded') continue;
    if (!current || Date.parse(script.created_at) > Date.parse(current.created_at))
      current = script;
  }
  return current;
}

export function deriveStage(input: WorkflowInput): StageId {
  const { project, sources, topics, jobs, scripts } = input;

  const script = currentScript(scripts);
  if (script) {
    switch (script.status) {
      case 'approved':
        return 'approved';
      case 'draft':
        return 'script_ready';
      case 'pending':
        return 'writing_script';
      case 'failed':
        return 'script_failed';
    }
  }
  // Without the script list we cannot tell whether a script exists, so say no more than "draft".
  if (scripts === undefined && jobs === undefined) return 'unknown';

  const extraction = latestJobOfKind(jobs, 'extract_topics');
  if (extraction && isActiveJob(extraction)) return 'extracting_topics';

  const hasDerivedTopics = topics?.some((topic) => topic.origin !== 'user') ?? false;
  if (extraction?.status === 'failed' && !hasDerivedTopics) return 'topics_failed';
  if (hasDerivedTopics || extraction?.status === 'succeeded') return 'topics_ready';

  if (isFileProject(project)) {
    const source = sources?.[0];
    if (sources === undefined) return 'unknown';
    if (!source) return 'needs_source';
    if (source.status === 'failed') return 'source_failed';
    if (source.status === 'pending' || source.status === 'processing') return 'reading_source';
    return 'ready_for_topics';
  }

  // A topic-only project starts from the person's own topics.
  if (topics === undefined) return 'unknown';
  return topics.length > 0 ? 'ready_for_topics' : 'needs_source';
}

function nextActionFor(stage: StageId, projectId: string): NextAction | null {
  const base = `/projects/${projectId}`;
  switch (stage) {
    case 'needs_source':
      return { label: 'Add source material', href: base };
    case 'source_failed':
      return { label: 'Upload a different file', href: base };
    case 'reading_source':
      return { label: 'View progress', href: `${base}/topics` };
    case 'ready_for_topics':
      return { label: 'Extract topics', href: `${base}/topics` };
    case 'extracting_topics':
      return { label: 'View progress', href: `${base}/topics` };
    case 'topics_failed':
      return { label: 'Review and try again', href: `${base}/topics` };
    case 'topics_ready':
      return { label: 'Review topics', href: `${base}/topics` };
    case 'writing_script':
      return { label: 'View progress', href: `${base}/script` };
    case 'script_ready':
      return { label: 'Review script', href: `${base}/script` };
    case 'script_failed':
      return { label: 'See what happened', href: `${base}/script` };
    case 'approved':
      return { label: 'View approved script', href: `${base}/script` };
    case 'unknown':
      return null;
  }
}

function fileDetail(source: Source | undefined, unit: string): string | null {
  if (!source) return null;
  const pages = source.page_count;
  return pages
    ? `${source.original_filename} · ${pages} ${pages === 1 ? unit : `${unit}s`}`
    : source.original_filename;
}

export function deriveWorkflow(input: WorkflowInput): Workflow {
  const { project, sources, topics, scripts } = input;
  const stage = deriveStage(input);
  const meta = STAGES[stage];
  const currentIndex = STAGE_STEP[stage];
  const script = currentScript(scripts);
  const source = sources?.[0];
  const usableTopicCount = topics?.filter((topic) => topic.status !== 'rejected').length ?? 0;

  const topicsSettled = stage === 'topics_ready' || currentIndex >= 2;
  const canOpenTopics = currentIndex >= 1 && stage !== 'unknown';
  const canOpenScript = currentIndex >= 2 || (stage === 'topics_ready' && usableTopicCount > 0);

  const reachable: Record<StepId, boolean> = {
    source: true,
    topics: canOpenTopics,
    script: canOpenScript,
    approved: stage === 'approved',
  };

  const details: Record<StepId, string | null> = {
    source: isFileProject(project)
      ? fileDetail(source, sourceWords(project).unit)
      : topics && topics.length > 0
        ? `${topics.length} ${topics.length === 1 ? 'keyword' : 'keywords'}`
        : null,
    topics:
      topicsSettled && topics
        ? `${usableTopicCount} ${usableTopicCount === 1 ? 'topic' : 'topics'}`
        : null,
    script: script ? STAGES[stage].label : null,
    approved: null,
  };

  const steps: Step[] = STEP_ORDER.map((id, index) => {
    let state: StepState;
    if (stage === 'approved') state = 'complete';
    else if (index < currentIndex) state = 'complete';
    else if (index === currentIndex) state = 'current';
    else state = 'upcoming';
    return {
      id,
      label: STEP_LABELS[id],
      state,
      attention: state === 'current' && FAILED_STAGES.has(stage),
      reachable: reachable[id],
      detail: details[id],
    };
  });

  return {
    stage,
    meta,
    steps,
    next: nextActionFor(stage, project.id),
    script,
    source,
    usableTopicCount,
    canOpenTopics,
    canOpenScript,
  };
}
