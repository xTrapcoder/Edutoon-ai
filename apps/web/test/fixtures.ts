import type {
  ClaimVerification,
  Job,
  Project,
  RenderEstimate,
  RenderOutput,
  RenderProgressStatus,
  RenderScene,
  RenderStatus,
  Scene,
  Script,
  ScriptDetail,
  SegmentAudio,
  SegmentStoryboard,
  Source,
  StoryboardStatus,
  SynthesisStatus,
  Topic,
} from '@edutoon/contracts';

/** Typed records for tests. Every one is built from the generated contract types. */

const AT = '2026-09-26T10:00:00.000Z';
const ID = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;

export const PROJECT_ID = ID(1);

export function project(over: Partial<Project> = {}): Project {
  return {
    id: PROJECT_ID,
    owner_id: ID(900),
    title: 'Computer Networks',
    description: null,
    source_type: 'pdf',
    status: 'draft',
    language: 'en-IN',
    orientation: 'horizontal',
    target_duration_ms: 300_000,
    instructions: null,
    instructions_notes: [],
    created_at: AT,
    updated_at: AT,
    ...over,
  };
}

export function source(over: Partial<Source> = {}): Source {
  return {
    id: ID(10),
    project_id: PROJECT_ID,
    uploader_id: ID(900),
    kind: 'pdf',
    original_filename: 'networks.pdf',
    content_type: 'application/pdf',
    byte_size: 1_000_000,
    checksum_sha256: 'a'.repeat(64),
    page_count: 42,
    status: 'parsed',
    extraction_method: 'text',
    ocr_page_count: null,
    created_at: AT,
    updated_at: AT,
    ...over,
  };
}

export function topic(over: Partial<Topic> = {}): Topic {
  return {
    id: ID(20),
    project_id: PROJECT_ID,
    parent_id: null,
    title: 'Network models',
    summary: 'The layered models used to describe networks.',
    keywords: ['OSI', 'TCP/IP'],
    position: 0,
    depth: 0,
    status: 'proposed',
    origin: 'source',
    evidence: [],
    created_at: AT,
    updated_at: AT,
    ...over,
  };
}

export function job(over: Partial<Job> = {}): Job {
  return {
    id: ID(30),
    project_id: PROJECT_ID,
    kind: 'extract_topics',
    status: 'succeeded',
    priority: 0,
    attempts: 1,
    max_attempts: 1,
    payload: {},
    result: null,
    error: null,
    scheduled_at: AT,
    started_at: AT,
    finished_at: AT,
    created_at: AT,
    updated_at: AT,
    ...over,
  };
}

export function script(over: Partial<Script> = {}): Script {
  return {
    id: ID(40),
    project_id: PROJECT_ID,
    status: 'draft',
    llm_model: 'test-model',
    estimated_duration_ms: 290_000,
    measured_duration_ms: null,
    created_at: AT,
    updated_at: AT,
    ...over,
  };
}

export function scriptDetail(over: Partial<ScriptDetail> = {}): ScriptDetail {
  return {
    ...script(),
    segments: [
      {
        id: ID(50),
        segment_index: 0,
        narration_text: 'A network connects computers so they can share resources.',
        estimated_duration_ms: 60_000,
        topic_id: null,
        created_at: AT,
        claims: [
          {
            id: ID(60),
            claim_text: 'A network connects computers.',
            status: 'supported',
            rationale: 'Stated on page 4 of the source.',
            created_at: AT,
          },
        ],
      },
    ],
    ...over,
  };
}

export function segmentAudio(over: Partial<SegmentAudio> = {}): SegmentAudio {
  return {
    status: 'ready',
    format: 'wav',
    sample_rate: 22050,
    channels: 1,
    measured_duration_ms: 58_000,
    voice_model: 'en_GB-cori-medium',
    is_temporary_voice_substitute: false,
    error: null,
    updated_at: AT,
    ...over,
  };
}

export function synthesisStatus(over: Partial<SynthesisStatus> = {}): SynthesisStatus {
  return {
    script: script({ status: 'approved' }),
    job: null,
    segments: [
      {
        id: ID(50),
        segment_index: 0,
        estimated_duration_ms: 60_000,
        audio: null,
      },
    ],
    ...over,
  };
}

export function scene(over: Partial<Scene> = {}): Scene {
  return {
    id: ID(80),
    scene_index: 0,
    start_ms: 0,
    duration_ms: 9_000,
    template: 'title_card',
    content: { title: 'A network connects computers' },
    reviewer_flag: false,
    updated_at: AT,
    ...over,
  };
}

export function segmentStoryboard(over: Partial<SegmentStoryboard> = {}): SegmentStoryboard {
  return {
    id: ID(50),
    segment_index: 0,
    expected_scene_count: 1,
    audio_measured_duration_ms: 60_000,
    is_stale: false,
    is_ready: false,
    scenes: [],
    ...over,
  };
}

export function storyboardStatus(over: Partial<StoryboardStatus> = {}): StoryboardStatus {
  return {
    script: script({ status: 'approved', measured_duration_ms: 60_000 }),
    job: null,
    segments: [segmentStoryboard()],
    ready_segment_count: 0,
    not_ready_segment_ids: [ID(50)],
    is_complete: false,
    timings_authoritative: true,
    dropped_visual_count: 0,
    estimated_provider_calls: 1,
    actual_provider_calls: null,
    ...over,
  };
}

export function renderScene(over: Partial<RenderScene> = {}): RenderScene {
  return {
    position: 0,
    segment_id: ID(50),
    segment_index: 0,
    scene_index: 0,
    scene_count: 1,
    template: 'title_card',
    start_ms: 0,
    duration_ms: 9_000,
    state: 'needs_render',
    ...over,
  };
}

export function renderEstimate(over: Partial<RenderEstimate> = {}): RenderEstimate {
  return {
    chunk_count: 1,
    unique_chunk_count: 1,
    chunks_reusable: 0,
    chunks_to_render: 1,
    total_ms: 9_000,
    width: 960,
    height: 540,
    fps: 30,
    estimated_output_bytes: 360_000,
    required_free_bytes: 5_000_000,
    ...over,
  };
}

export function renderOutput(over: Partial<RenderOutput> = {}): RenderOutput {
  return {
    orientation: 'horizontal',
    quality: 'low',
    width: 960,
    height: 540,
    fps: 30,
    duration_ms: 9_000,
    byte_size: 1_200_000,
    encoder: 'libx264',
    renderer_version: 'pillow-ffmpeg/1',
    created_at: AT,
    updated_at: AT,
    is_current: true,
    ...over,
  };
}

/** A script with one rendered-nothing scene: the state before anyone has pressed "Render". */
export function renderStatus(over: Partial<RenderStatus> = {}): RenderStatus {
  return {
    script: script({ status: 'approved', measured_duration_ms: 60_000 }),
    orientation: 'horizontal',
    quality: 'low',
    job: null,
    progress: null,
    blockers: [],
    scenes: [renderScene()],
    ready_count: 0,
    needs_render_count: 1,
    stale_count: 0,
    failed_count: 0,
    output: null,
    output_is_current: false,
    estimate: renderEstimate(),
    total_ms: 9_000,
    is_complete: false,
    ...over,
  };
}

export function renderProgressStatus(
  over: Partial<RenderProgressStatus> = {},
): RenderProgressStatus {
  return { job: null, progress: null, ...over };
}

export function claim(over: Partial<ClaimVerification> = {}): ClaimVerification {
  return {
    id: ID(70),
    project_id: PROJECT_ID,
    claim_text: 'A network connects computers.',
    status: 'supported',
    rationale: null,
    embedding_model: 'test',
    llm_model: 'test',
    topic_id: null,
    created_at: AT,
    citations: [],
    ...over,
  };
}
