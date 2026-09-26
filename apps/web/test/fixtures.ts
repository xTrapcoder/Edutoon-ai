import type {
  ClaimVerification,
  Job,
  Project,
  Script,
  ScriptDetail,
  Source,
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

export function claim(over: Partial<ClaimVerification> = {}): ClaimVerification {
  return {
    id: ID(70),
    project_id: PROJECT_ID,
    claim_text: 'A network connects computers.',
    status: 'supported',
    rationale: null,
    embedding_model: 'test',
    llm_model: 'test',
    created_at: AT,
    citations: [],
    ...over,
  };
}
