/**
 * A throwaway mock of the EduToon API for UI QA. Test tooling only: it is never part of a build
 * and nothing in the app imports it.
 *
 *   node test-support/mock-api/server.ts                # port 8787, a handful of projects
 *   MOCK_SCENARIO=empty  node test-support/mock-api/server.ts   # a brand-new account
 *   MOCK_SCENARIO=errors node test-support/mock-api/server.ts   # every /v1 call fails with a 500
 *
 * It mimics the real API's shapes (see packages/contracts/openapi.json) and rules that matter to
 * the UI: bearer token required, Idempotency-Key required on writes, `{ error: { code, message } }`
 * failures, jobs that move queued -> running -> succeeded, and "no supported claims" refusing a
 * script. The sample content is fixture text written for these checks, not product data.
 */
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { randomUUID } from 'node:crypto';

const PORT = Number(process.env.MOCK_API_PORT ?? 8787);
const ORIGIN = process.env.MOCK_ALLOWED_ORIGIN ?? 'http://localhost:3000';
const LATENCY_MS = Number(process.env.MOCK_LATENCY_MS ?? 250);
const SCENARIO = process.env.MOCK_SCENARIO ?? 'default';
/** Seconds a job stays queued, then running, before it finishes. */
const QUEUE_S = Number(process.env.MOCK_QUEUE_S ?? 4);
const RUN_S = Number(process.env.MOCK_RUN_S ?? 10);

type Row = Record<string, unknown> & { id: string };
const now = () => new Date().toISOString();
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();
const MIN = 60_000;
const uuid = () => randomUUID();

const OWNER = uuid();

/** A real, valid, silent 16-bit PCM mono WAV of the given duration - so the QA player shows a
 * genuine, correct-length audio element rather than a broken one. */
function silentWav(durationMs: number): Buffer {
  const sampleRate = 22050;
  const frames = Math.max(1, Math.round((durationMs / 1000) * sampleRate));
  const dataSize = frames * 2; // 16-bit mono
  const buf = Buffer.alloc(44 + dataSize);
  buf.write('RIFF', 0);
  buf.writeUInt32LE(36 + dataSize, 4);
  buf.write('WAVE', 8);
  buf.write('fmt ', 12);
  buf.writeUInt32LE(16, 16);
  buf.writeUInt16LE(1, 20); // PCM
  buf.writeUInt16LE(1, 22); // mono
  buf.writeUInt32LE(sampleRate, 24);
  buf.writeUInt32LE(sampleRate * 2, 28); // byte rate
  buf.writeUInt16LE(2, 32); // block align
  buf.writeUInt16LE(16, 34); // bits per sample
  buf.write('data', 36);
  buf.writeUInt32LE(dataSize, 40);
  return buf; // PCM samples left zeroed = silence
}

// --- state --------------------------------------------------------------------------------

const projects: Row[] = [];
const sources: Row[] = [];
const chunks: Row[] = [];
const topics: Row[] = [];
const jobs: Row[] = [];
const scripts: Row[] = [];
const segments: Row[] = [];
const segmentAudio: Row[] = [];
const claims: Row[] = [];
/** Idempotency: `${method} ${path} ${key}` -> the first response. */
const replays = new Map<string, { status: number; body: unknown }>();

function project(over: Partial<Row>): Row {
  const row: Row = {
    id: uuid(),
    owner_id: OWNER,
    title: 'Untitled',
    description: null,
    source_type: 'pdf',
    status: 'draft',
    language: 'en-IN',
    orientation: 'horizontal',
    target_duration_ms: 5 * MIN,
    created_at: ago(2 * 86_400_000),
    updated_at: ago(2 * 86_400_000),
    ...over,
  };
  projects.push(row);
  return row;
}

function source(projectId: string, name: string, pages: number, over: Partial<Row> = {}): Row {
  const row: Row = {
    id: uuid(),
    project_id: projectId,
    uploader_id: OWNER,
    kind: 'pdf',
    original_filename: name,
    content_type: 'application/pdf',
    byte_size: 2_412_000,
    checksum_sha256: randomUUID().replaceAll('-', ''),
    page_count: pages,
    status: 'parsed',
    created_at: ago(2 * 86_400_000),
    updated_at: ago(2 * 86_400_000),
    ...over,
  };
  sources.push(row);
  return row;
}

function job(projectId: string, kind: string, status: string, over: Partial<Row> = {}): Row {
  const row: Row = {
    id: uuid(),
    project_id: projectId,
    kind,
    status,
    priority: 0,
    attempts: status === 'queued' ? 0 : 1,
    max_attempts: 1,
    payload: {},
    result: null,
    error: null,
    scheduled_at: now(),
    started_at: null,
    finished_at: null,
    created_at: now(),
    updated_at: now(),
    ...over,
  };
  jobs.unshift(row);
  return row;
}

function topic(
  projectId: string,
  title: string,
  over: Partial<Row> & { pages?: [number, number][]; chunkIds?: string[]; sourceId?: string } = {},
): Row {
  const { pages, chunkIds, sourceId, ...rest } = over;
  const parentId = (rest.parent_id as string | null | undefined) ?? null;
  const siblings = topics.filter((t) => t.project_id === projectId && t.parent_id === parentId);
  const row: Row = {
    id: uuid(),
    project_id: projectId,
    parent_id: parentId,
    title,
    summary: null,
    keywords: [],
    position: siblings.length,
    depth: parentId ? (topics.find((t) => t.id === parentId)?.depth as number) + 1 : 0,
    status: 'proposed',
    origin: 'source',
    evidence: (pages ?? []).map(([from, to], i) => ({
      source_chunk_id: chunkIds?.[i] ?? uuid(),
      source_id: sourceId ?? uuid(),
      page_from: from,
      page_to: to,
    })),
    created_at: ago(86_400_000),
    updated_at: ago(86_400_000),
    ...rest,
  };
  topics.push(row);
  return row;
}

// Sample passages for the evidence panel. Fixture text for these checks.
const PASSAGES = [
  'A computer network is a collection of interconnected computers and devices that can communicate and share resources with one another. Networks are classified by their size and purpose: a personal area network connects devices within a few metres, a local area network covers a building, and a wide area network spans cities or countries.',
  'The OSI reference model divides network communication into seven layers: physical, data link, network, transport, session, presentation and application. Each layer provides services to the layer above it and relies on the layer below, which allows protocols at different layers to be designed and replaced independently.',
  'The TCP/IP model has four layers: link, internet, transport and application. The internet layer uses IP addresses to deliver packets between hosts, while the transport layer provides either reliable, ordered delivery through TCP or lightweight delivery through UDP.',
  'A switch forwards frames between devices on the same network using MAC addresses, learning which port each address is reachable through. A router connects different networks and forwards packets between them by consulting a routing table built from routing protocols or static entries.',
];

function seedNetworks(): Row {
  const p = project({
    title: 'Computer Networks Fundamentals',
    updated_at: ago(2 * 3_600_000),
  });
  const s = source(p.id, 'computer_networks_notes.pdf', 42);
  const ch = PASSAGES.map((content, i) => {
    const row: Row = {
      id: uuid(),
      source_id: s.id,
      project_id: p.id,
      chunk_index: i,
      page_from: 4 + i * 9,
      page_to: 8 + i * 9,
      content,
      token_count: 80,
      created_at: ago(86_400_000),
      updated_at: ago(86_400_000),
    };
    chunks.push(row);
    return row;
  });
  const cid = (i: number) => ch[i]!.id;
  const t = (title: string, over: Parameters<typeof topic>[2] = {}) =>
    topic(p.id, title, { sourceId: s.id, ...over });

  const intro = t('Introduction', {
    status: 'accepted',
    summary:
      'What a computer network is, why networks matter, and how they are classified by size and purpose.',
    keywords: ['computer network', 'PAN', 'LAN', 'WAN'],
    pages: [[4, 8]],
    chunkIds: [cid(0)],
  });
  t('Types of networks', {
    parent_id: intro.id,
    status: 'accepted',
    summary: 'Personal, local, metropolitan and wide area networks compared.',
    pages: [[5, 6]],
    chunkIds: [cid(0)],
  });
  t('History and evolution', {
    parent_id: intro.id,
    summary: 'How networking grew from ARPANET to the modern internet.',
    pages: [[7, 8]],
    chunkIds: [cid(0)],
  });
  const models = t('Network models', {
    status: 'accepted',
    summary: 'The layered models used to describe and design network communication.',
    keywords: ['OSI', 'TCP/IP', 'layers'],
    pages: [[13, 17]],
    chunkIds: [cid(1)],
  });
  t('The OSI model', {
    parent_id: models.id,
    status: 'accepted',
    summary: 'Seven layers, from physical signalling to applications.',
    pages: [[13, 15]],
    chunkIds: [cid(1)],
  });
  t('The TCP/IP model', {
    parent_id: models.id,
    status: 'accepted',
    summary: 'The four-layer model the internet actually runs on.',
    pages: [[15, 17]],
    chunkIds: [cid(2)],
  });
  const devices = t('Network devices', {
    summary: 'The hardware that moves data between machines.',
    keywords: ['switch', 'router'],
    pages: [[22, 26]],
    chunkIds: [cid(3)],
  });
  t('Switches and routers', {
    parent_id: devices.id,
    summary: 'How frames and packets are forwarded.',
    pages: [[23, 25]],
    chunkIds: [cid(3)],
  });
  t('Firewalls', {
    parent_id: devices.id,
    origin: 'suggested',
    summary: 'A common security device. Suggested to round out the outline.',
  });
  t('Network security basics', { origin: 'user', status: 'accepted' });

  job(p.id, 'build_evidence', 'succeeded', {
    created_at: ago(86_400_000),
    finished_at: ago(86_400_000),
  });
  job(p.id, 'extract_topics', 'succeeded', {
    created_at: ago(86_300_000),
    result: { mode: 'pdf', topic_count: 10, chunks_total: 4, chunks_used: 4, truncated: false },
  });
  const sc = script(p.id, 'approved', 6 * MIN);
  const titles = [
    'Introduction',
    'Types of networks',
    'The OSI model',
    'The TCP/IP model',
    'Switches and routers',
    'Wrapping up',
  ];
  titles.forEach((title, i) => {
    const tp = topics.find((x) => x.project_id === p.id && x.title === title);
    segment(
      sc.id,
      i,
      tp?.id ?? null,
      i === 5
        ? 'To bring it together: networks let devices share resources, and layered models keep that communication organised. In the next lesson we look at how addresses are assigned.'
        : `${PASSAGES[i % PASSAGES.length]}\n\nThat is the key idea for this section: ${title.toLowerCase()} is easiest to understand as a set of small, well-defined responsibilities.`,
      [
        {
          claim_text: PASSAGES[i % PASSAGES.length]!.split('. ')[0]! + '.',
          status: 'supported',
          rationale: 'This sentence appears, in substance, on the cited page of your document.',
        },
      ],
    );
  });
  return p;
}

function script(projectId: string, status: string, estimated: number | null): Row {
  const row: Row = {
    id: uuid(),
    project_id: projectId,
    status,
    llm_model: 'claude-sonnet-5',
    estimated_duration_ms: estimated,
    measured_duration_ms: null,
    created_at: ago(3_600_000),
    updated_at: ago(3_000_000),
  };
  scripts.unshift(row);
  return row;
}

// Piper has no genuine Indian-English voice (see apps/api/README.md, "Text-to-speech"): en-IN
// mirrors that real limitation here too, rather than a mock that quietly does better than the
// real system.
const EN_GB_VOICE = 'en_GB-cori-medium';
const voiceForLanguage = (language: string) => ({
  voiceModel: EN_GB_VOICE,
  isTemporarySubstitute: language === 'en-IN',
});

function readySegmentAudio(seg: Row, projectLanguage: string): Row {
  const { voiceModel, isTemporarySubstitute } = voiceForLanguage(projectLanguage);
  const measured = Math.round((seg.estimated_duration_ms as number) * (0.9 + Math.random() * 0.2));
  const row: Row = {
    id: uuid(),
    script_segment_id: seg.id,
    status: 'ready',
    format: 'wav',
    sample_rate: 22050,
    channels: 1,
    measured_duration_ms: measured,
    voice_model: voiceModel,
    is_temporary_voice_substitute: isTemporarySubstitute,
    error: null,
    updated_at: now(),
  };
  segmentAudio.push(row);
  return row;
}

function segment(
  scriptId: string,
  index: number,
  topicId: string | null,
  text: string,
  cited: Partial<Row>[],
): Row {
  const row: Row = {
    id: uuid(),
    script_id: scriptId,
    segment_index: index,
    narration_text: text,
    estimated_duration_ms: 55_000 + index * 6_000,
    topic_id: topicId,
    created_at: ago(3_000_000),
    claims: cited.map((c) => ({ id: uuid(), created_at: ago(3_000_000), rationale: null, ...c })),
  };
  segments.push(row);
  return row;
}

function seedDefault() {
  seedNetworks();

  // Keywords project with an extraction that is running right now.
  const ml = project({
    title: 'Machine Learning Basics',
    source_type: 'topic',
    updated_at: ago(30 * MIN),
    target_duration_ms: 10 * MIN,
    language: 'en-GB',
  });
  ['Supervised learning', 'Neural networks', 'Overfitting'].forEach((k) =>
    topic(ml.id, k, { origin: 'user', status: 'accepted' }),
  );
  job(ml.id, 'extract_topics', 'running', {
    created_at: ago(40_000),
    started_at: ago(28_000),
    payload: { hold: true },
  });

  // Topics reviewed, no verified claims yet: the honest "verification required" path.
  const db = project({
    title: 'Databases Primer',
    updated_at: ago(5 * 3_600_000),
    orientation: 'vertical',
    target_duration_ms: 3 * MIN,
  });
  const dbs = source(db.id, 'databases_primer.pdf', 18, { created_at: ago(6 * 3_600_000) });
  topic(db.id, 'Relational model', {
    sourceId: dbs.id,
    pages: [[2, 4]],
    summary: 'Tables, rows and keys.',
    status: 'accepted',
  });
  topic(db.id, 'SQL basics', {
    sourceId: dbs.id,
    pages: [[6, 9]],
    summary: 'Querying and changing data.',
  });
  job(db.id, 'extract_topics', 'succeeded', {
    created_at: ago(5 * 3_600_000),
    result: { mode: 'pdf', topic_count: 2, chunks_total: 6, chunks_used: 6, truncated: false },
  });

  // A failed extraction, with the API's own plain-language reason.
  const dsa = project({ title: 'Data Structures and Algorithms', updated_at: ago(4 * 86_400_000) });
  source(dsa.id, 'dsa_lecture_notes.pdf', 120);
  job(dsa.id, 'extract_topics', 'failed', {
    created_at: ago(4 * 86_400_000),
    error: 'The model is busy right now. Please try again in a few minutes.',
  });

  // A script generation in progress, mid-topic - the progress card's dynamic label.
  const gen = project({
    title: 'Cell Biology',
    updated_at: ago(2 * MIN),
    target_duration_ms: 8 * MIN,
  });
  const gensrc = source(gen.id, 'cell_biology.pdf', 30, { created_at: ago(2 * 3_600_000) });
  topic(gen.id, 'The cell membrane', {
    sourceId: gensrc.id,
    pages: [[3, 6]],
    summary: 'Structure and function of the membrane.',
    status: 'accepted',
  });
  topic(gen.id, 'Mitochondria', {
    sourceId: gensrc.id,
    pages: [[10, 13]],
    summary: "The cell's powerhouse.",
    status: 'accepted',
  });
  const genScript = script(gen.id, 'pending', null);
  job(gen.id, 'generate_script', 'running', {
    created_at: ago(45_000),
    started_at: ago(40_000),
    payload: {
      hold: true,
      script_id: genScript.id,
      project_id: gen.id,
      topic_ids: [],
      progress: {
        stage: 'generating_topic',
        topic_index: 1,
        topic_count: 2,
        topic_title: 'Mitochondria',
        updated_at: now(),
      },
    },
  });

  // Nothing added yet.
  project({ title: 'Operating Systems', updated_at: ago(10 * MIN), created_at: ago(10 * MIN) });
}

// --- behaviour ---------------------------------------------------------------------------

const outline = [
  ['Overview', 'What the subject is and why it matters.', [[2, 5]]],
  ['Core ideas', 'The central concepts, one at a time.', [[6, 14]]],
  ['Worked examples', 'How the ideas are used in practice.', [[15, 22]]],
] as const;

function finishExtraction(job_: Row) {
  const projectId = job_.project_id as string;
  const src = sources.find((s) => s.project_id === projectId);
  const replace = (job_.payload as { replace?: boolean }).replace === true;
  if (replace) {
    for (let i = topics.length - 1; i >= 0; i--) {
      if (topics[i]!.project_id === projectId && topics[i]!.origin !== 'user') topics.splice(i, 1);
    }
  }
  for (const [title, summary, pages] of outline) {
    const parent = topic(projectId, title, {
      summary,
      sourceId: src?.id as string | undefined,
      pages: src ? (pages as unknown as [number, number][]) : [],
      origin: src ? 'source' : 'suggested',
    });
    topic(projectId, `${title}: key terms`, {
      parent_id: parent.id,
      sourceId: src?.id as string | undefined,
      pages: src ? [pages[0] as unknown as [number, number]] : [],
      origin: src ? 'source' : 'suggested',
    });
  }
  job_.status = 'succeeded';
  job_.finished_at = now();
  job_.result = {
    mode: src ? 'pdf' : 'keywords',
    topic_count: topics.filter((t) => t.project_id === projectId).length,
    chunks_total: 6,
    chunks_used: 6,
    truncated: false,
  };
}

/** Topics that count as accepted for claim extraction - a topic counts only if it and every
 * ancestor is accepted, mirroring `domain.topic_tree.accepted_topics`. */
function eligibleAcceptedTopics(projectId: string): Row[] {
  const acceptedIds = new Set<string>();
  const result: Row[] = [];
  for (const t of topics.filter((x) => x.project_id === projectId)) {
    const parentId = t.parent_id as string | null;
    const parentCounts = parentId === null || acceptedIds.has(parentId);
    if (t.status === 'accepted' && parentCounts) {
      acceptedIds.add(t.id);
      result.push(t);
    }
  }
  return result;
}

const CLAIM_TEMPLATES: [string, string, string][] = [
  [
    '{title} plays the role described in your source material.',
    'supported',
    'Found on the cited pages of your document.',
  ],
  [
    '{title} was first standardised in 1969.',
    'unsupported',
    'No indexed source material supports this statement.',
  ],
];

function finishClaimExtraction(job_: Row) {
  const projectId = job_.project_id as string;
  const replace = (job_.payload as { replace?: boolean }).replace === true;
  const eligible = eligibleAcceptedTopics(projectId);
  let processed = 0;
  let skipped = 0;
  let candidatesGenerated = 0;
  let claimsVerified = 0;
  const byStatus: Record<string, number> = {};

  for (const t of eligible) {
    const already = claims.some((c) => c.topic_id === t.id);
    if (already && !replace) {
      skipped += 1;
      continue;
    }
    if (already && replace) {
      for (let i = claims.length - 1; i >= 0; i--) {
        if (claims[i]!.topic_id === t.id) claims.splice(i, 1);
      }
    }
    processed += 1;
    for (const [template, status, rationale] of CLAIM_TEMPLATES) {
      candidatesGenerated += 1;
      claims.push({
        id: uuid(),
        project_id: projectId,
        claim_text: template.replace('{title}', t.title as string),
        status,
        rationale,
        embedding_model: 'mock',
        llm_model: 'mock',
        topic_id: t.id,
        created_at: now(),
        citations: [],
      });
      claimsVerified += 1;
      byStatus[status] = (byStatus[status] ?? 0) + 1;
    }
  }

  job_.status = 'succeeded';
  job_.finished_at = now();
  job_.result = {
    topics_eligible: eligible.length,
    topics_processed: processed,
    topics_skipped_already_extracted: skipped,
    topics_failed: 0,
    candidates_generated: candidatesGenerated,
    claims_verified: claimsVerified,
    claims_by_status: byStatus,
    failures: [],
    replace,
  };
}

function advance(job_: Row) {
  // A job seeded with `hold` stays exactly as it is, so the in-progress screens can be inspected.
  if ((job_.payload as { hold?: boolean }).hold) return;
  const age = (Date.now() - Date.parse(job_.created_at as string)) / 1000;
  if (job_.status === 'queued' && age > QUEUE_S) {
    job_.status = 'running';
    job_.started_at = now();
  }
  if (job_.status === 'running' && age > QUEUE_S + RUN_S) {
    if (job_.kind === 'extract_topics') finishExtraction(job_);
    if (job_.kind === 'extract_claims') finishClaimExtraction(job_);
    if (job_.kind === 'generate_script') {
      const sc = scripts.find((s) => s.id === (job_.payload as { script_id: string }).script_id);
      if (sc) {
        sc.status = 'draft';
        sc.estimated_duration_ms = 5 * MIN;
        sc.updated_at = now();
        const chosen = topics
          .filter((t) => t.project_id === job_.project_id && t.status !== 'rejected')
          .slice(0, 4);
        chosen.forEach((t, i) =>
          segment(
            sc.id,
            i,
            t.id,
            `${t.title}. ${(t.summary as string | null) ?? 'This section covers the topic in the words of your source.'}\n\nWe will take it step by step so each idea is clear before we move on.`,
            [
              {
                claim_text: `${t.title} is covered in the source material.`,
                status: 'supported',
                rationale: 'Found on the cited pages of your document.',
              },
            ],
          ),
        );
      }
      job_.status = 'succeeded';
      job_.finished_at = now();
    }
    if (job_.kind === 'synthesise_voice') {
      const sc = scripts.find((s) => s.id === (job_.payload as { script_id: string }).script_id);
      const p = projects.find((x) => x.id === job_.project_id);
      if (sc && p) {
        const segs = segments
          .filter((s) => s.script_id === sc.id)
          .sort((x, y) => (x.segment_index as number) - (y.segment_index as number));
        for (let i = segmentAudio.length - 1; i >= 0; i--) {
          if (segs.some((s) => s.id === segmentAudio[i]!.script_segment_id)) segmentAudio.splice(i, 1);
        }
        const created = segs.map((seg) => readySegmentAudio(seg, p.language as string));
        sc.measured_duration_ms = created.reduce(
          (total, a) => total + (a.measured_duration_ms as number),
          0,
        );
        sc.updated_at = now();
      }
      job_.status = 'succeeded';
      job_.finished_at = now();
      job_.result = {
        script_id: (job_.payload as { script_id: string }).script_id,
        segment_count: segments.filter((s) => s.script_id === sc?.id).length,
        ready_segment_count: segments.filter((s) => s.script_id === sc?.id).length,
        failed_segment_ids: [],
        measured_duration_ms: sc?.measured_duration_ms ?? null,
      };
    }
  }
}

function tick() {
  jobs.filter((j) => j.status === 'queued' || j.status === 'running').forEach(advance);
}

// --- http --------------------------------------------------------------------------------

function send(res: ServerResponse, status: number, body?: unknown) {
  res.writeHead(status, {
    'Access-Control-Allow-Origin': ORIGIN,
    'Access-Control-Allow-Credentials': 'true',
    'Access-Control-Allow-Headers': 'authorization, content-type, idempotency-key',
    'Access-Control-Allow-Methods': 'GET, POST, PATCH, DELETE, OPTIONS',
    'Access-Control-Expose-Headers': 'X-Request-Id',
    'X-Request-Id': randomUUID(),
    ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
  });
  res.end(body === undefined ? undefined : JSON.stringify(body));
}

const fail = (res: ServerResponse, status: number, code: string, message: string) =>
  send(res, status, { error: { code, message } });

async function readBody(req: IncomingMessage): Promise<Buffer> {
  const parts: Buffer[] = [];
  for await (const part of req) parts.push(part as Buffer);
  return Buffer.concat(parts);
}

const page = (items: Row[]) => ({ items, next_cursor: null });

async function route(req: IncomingMessage, res: ServerResponse) {
  const url = new URL(req.url ?? '/', 'http://mock');
  const method = req.method ?? 'GET';
  const path = url.pathname;

  if (method === 'OPTIONS') return send(res, 204);
  if (path === '/health') return send(res, 200, { status: 'ok', mock: true });
  if (!path.startsWith('/v1/')) return fail(res, 404, 'not_found', 'Not found.');
  if (!req.headers.authorization?.startsWith('Bearer '))
    return fail(res, 401, 'unauthorized', 'Missing Authorization header.');
  if (SCENARIO === 'errors')
    return fail(res, 500, 'internal_error', 'An unexpected error occurred.');

  tick();

  const mutating = method === 'POST' || method === 'PATCH' || method === 'DELETE';
  const key = req.headers['idempotency-key'];
  if (mutating && (typeof key !== 'string' || key.length < 1 || key.length > 255)) {
    return send(res, 422, {
      error: {
        code: 'VALIDATION_ERROR',
        message: 'Request validation failed.',
        details: {
          field_errors: [
            { path: 'header.idempotency-key', message: 'Field required', type: 'missing' },
          ],
        },
      },
    });
  }
  const replayKey = `${method} ${path} ${String(key)}`;
  if (mutating && replays.has(replayKey)) {
    const first = replays.get(replayKey)!;
    return send(res, first.status, first.body);
  }
  const done = (status: number, body?: unknown) => {
    if (mutating && status < 400) replays.set(replayKey, { status, body });
    send(res, status, body);
  };

  const m = (pattern: RegExp) => path.match(pattern);
  let hit: RegExpMatchArray | null;

  // Five path segments (project/scripts/id/segments/id/audio) - outside the generic resource
  // regex below, which only captures two - so it is matched here first.
  if (
    (hit = m(/^\/v1\/projects\/([^/]+)\/scripts\/([^/]+)\/segments\/([^/]+)\/audio$/)) &&
    method === 'GET'
  ) {
    const [, pid, sid, segId] = hit as unknown as [string, string, string, string];
    const p = projects.find((x) => x.id === pid);
    if (!p) return fail(res, 404, 'not_found', `Project ${pid} not found.`);
    const sc = scripts.find((s) => s.id === sid && s.project_id === pid);
    if (!sc) return fail(res, 404, 'not_found', `Script ${sid} not found.`);
    const audio = segmentAudio.find((a) => a.script_segment_id === segId);
    if (!audio || audio.status !== 'ready')
      return fail(res, 404, 'not_found', `Audio for segment ${segId} not found.`);
    const wav = silentWav((audio.measured_duration_ms as number) ?? 1000);
    res.writeHead(200, {
      'Access-Control-Allow-Origin': ORIGIN,
      'Access-Control-Allow-Credentials': 'true',
      'Content-Type': 'audio/wav',
      'Content-Length': String(wav.length),
      'Accept-Ranges': 'bytes',
    });
    return res.end(wav);
  }

  if (path === '/v1/projects' && method === 'GET')
    return send(
      res,
      200,
      page(
        [...projects].sort(
          (a, b) => Date.parse(b.updated_at as string) - Date.parse(a.updated_at as string),
        ),
      ),
    );
  if (path === '/v1/projects' && method === 'POST') {
    const body = JSON.parse((await readBody(req)).toString() || '{}') as Partial<Row>;
    if (typeof body.title !== 'string' || body.title.trim() === '')
      return send(res, 422, {
        error: { code: 'VALIDATION_ERROR', message: 'Request validation failed.' },
      });
    const p = project({
      title: body.title,
      source_type: body.source_type as string,
      language: body.language ?? 'en-IN',
      orientation: body.orientation ?? 'horizontal',
      target_duration_ms: (body.target_duration_ms as number) ?? 5 * MIN,
      created_at: now(),
      updated_at: now(),
    });
    return done(201, p);
  }

  if ((hit = m(/^\/v1\/projects\/([^/]+)$/))) {
    const p = projects.find((x) => x.id === hit![1]);
    if (!p) return fail(res, 404, 'not_found', `Project ${hit[1]} not found.`);
    if (method === 'GET') return send(res, 200, p);
    if (method === 'PATCH') {
      Object.assign(p, JSON.parse((await readBody(req)).toString() || '{}'), { updated_at: now() });
      return done(200, p);
    }
    if (method === 'DELETE') {
      projects.splice(projects.indexOf(p), 1);
      return done(204);
    }
  }

  if (
    (hit = m(
      /^\/v1\/projects\/([^/]+)\/(sources|topics|jobs|scripts|claim-verifications)(?:\/([^/]+))?(?:\/([^/]+))?$/,
    ))
  ) {
    const [, pid, resource, a, b] = hit as unknown as [string, string, string, string?, string?];
    const p = projects.find((x) => x.id === pid);
    if (!p) return fail(res, 404, 'not_found', `Project ${pid} not found.`);
    const touch = () => (p.updated_at = now());

    if (resource === 'sources') {
      if (method === 'GET' && !a)
        return send(res, 200, page(sources.filter((s) => s.project_id === pid).reverse()));
      if (method === 'GET' && b === 'chunks')
        return send(res, 200, { items: chunks.filter((c) => c.source_id === a) });
      if (method === 'POST' && !a) {
        const raw = (await readBody(req)).toString('latin1');
        const name = /filename="([^"]+)"/.exec(raw)?.[1] ?? 'upload.pdf';
        if (!raw.includes('%PDF'))
          return send(res, 422, {
            error: { code: 'VALIDATION_ERROR', message: 'Request validation failed.' },
          });
        const s = source(pid, name, 24, {
          created_at: now(),
          updated_at: now(),
          byte_size: raw.length,
        });
        touch();
        return done(201, s);
      }
    }

    if (resource === 'topics') {
      if (method === 'GET' && !a)
        return send(res, 200, { items: topics.filter((t) => t.project_id === pid) });
      if (method === 'POST' && !a) {
        const { keywords } = JSON.parse((await readBody(req)).toString() || '{}') as {
          keywords: string[];
        };
        if (
          jobs.some(
            (j) =>
              j.project_id === pid &&
              j.kind === 'extract_topics' &&
              (j.status === 'queued' || j.status === 'running'),
          )
        )
          return fail(
            res,
            409,
            'conflict',
            'Topic extraction is already queued or running for this project; wait for it to finish.',
          );
        const made = keywords.map((k) =>
          topic(pid, k, {
            origin: 'user',
            status: 'accepted',
            created_at: now(),
            updated_at: now(),
          }),
        );
        touch();
        return done(201, { items: made });
      }
      if (method === 'POST' && a === 'extraction') {
        const body = JSON.parse((await readBody(req)).toString() || '{}') as { replace?: boolean };
        if (
          jobs.some(
            (j) =>
              j.project_id === pid &&
              j.kind === 'extract_topics' &&
              (j.status === 'queued' || j.status === 'running'),
          )
        )
          return fail(
            res,
            409,
            'conflict',
            'Topic extraction is already queued or running for this project; wait for it to finish.',
          );
        if (!body.replace && topics.some((t) => t.project_id === pid && t.origin !== 'user'))
          return fail(
            res,
            409,
            'conflict',
            'Topics have already been extracted for this project. Run the extraction again with replace to overwrite the derived topics (your own topics are kept).',
          );
        if (!sources.some((s) => s.project_id === pid) && !topics.some((t) => t.project_id === pid))
          return fail(
            res,
            400,
            'validation_error',
            'There is nothing to extract topics from yet. Upload a PDF or add keywords first.',
          );
        const j = job(pid, 'extract_topics', 'queued', {
          payload: { project_id: pid, replace: body.replace === true },
        });
        touch();
        return done(202, j);
      }
      if (method === 'PATCH' && a) {
        const t = topics.find((x) => x.id === a && x.project_id === pid);
        if (!t) return fail(res, 404, 'not_found', `Topic ${a} not found.`);
        const patch = JSON.parse((await readBody(req)).toString() || '{}') as Partial<Row>;
        if (
          ('title' in patch || 'summary' in patch) &&
          jobs.some(
            (j) =>
              j.project_id === pid &&
              j.kind === 'extract_topics' &&
              (j.status === 'queued' || j.status === 'running'),
          )
        )
          return fail(
            res,
            409,
            'conflict',
            'Topic extraction is running; wait for it to finish before changing wording.',
          );
        Object.assign(t, patch, { updated_at: now() });
        touch();
        return done(200, t);
      }
    }

    if (resource === 'jobs' && method === 'GET')
      return send(res, 200, page(jobs.filter((j) => j.project_id === pid)));

    if (resource === 'claim-verifications') {
      if (method === 'GET' && !a) {
        // MOCK_WITH_CLAIMS=1 pretends a project has one verified claim, so script generation can be tried.
        const withClaim =
          process.env.MOCK_WITH_CLAIMS === '1'
            ? [
                {
                  id: uuid(),
                  project_id: pid,
                  claim_text: 'A sample verified claim.',
                  status: 'supported',
                  rationale: null,
                  embedding_model: 'mock',
                  llm_model: 'mock',
                  topic_id: null,
                  created_at: now(),
                  citations: [],
                },
              ]
            : [];
        return send(
          res,
          200,
          page([...claims.filter((c) => c.project_id === pid), ...withClaim] as Row[]),
        );
      }
      if (method === 'POST' && a === 'extraction') {
        const body = JSON.parse((await readBody(req)).toString() || '{}') as { replace?: boolean };
        if (
          jobs.some(
            (j) =>
              j.project_id === pid &&
              j.kind === 'extract_claims' &&
              (j.status === 'queued' || j.status === 'running'),
          )
        )
          return fail(
            res,
            409,
            'conflict',
            'Claim extraction is already queued or running for this project; wait for it to finish.',
          );
        if (eligibleAcceptedTopics(pid).length === 0)
          return fail(
            res,
            400,
            'validation_error',
            'There are no accepted topics to extract claims from. Accept at least one topic first.',
          );
        const j = job(pid, 'extract_claims', 'queued', {
          payload: { project_id: pid, replace: body.replace === true },
        });
        touch();
        return done(202, j);
      }
    }

    if (resource === 'scripts') {
      const mine = scripts.filter((s) => s.project_id === pid);

      if (a && b === 'synthesis') {
        const sc = mine.find((s) => s.id === a);
        if (!sc) return fail(res, 404, 'not_found', `Script ${a} not found.`);
        if (method === 'POST') {
          if (sc.status !== 'approved')
            return fail(
              res,
              409,
              'conflict',
              `Only an approved script can be synthesised; this one is '${sc.status}'.`,
            );
          if (
            jobs.some(
              (j) =>
                j.project_id === pid &&
                j.kind === 'synthesise_voice' &&
                (j.status === 'queued' || j.status === 'running'),
            )
          )
            return fail(
              res,
              409,
              'conflict',
              'Synthesis is already queued or running for this project; wait for it to finish.',
            );
          const j = job(pid, 'synthesise_voice', 'queued', {
            payload: { script_id: sc.id, project_id: pid },
          });
          touch();
          return done(202, j);
        }
        if (method === 'GET') {
          const latest = jobs
            .filter((j) => j.project_id === pid && j.kind === 'synthesise_voice')
            .sort((x, y) => Date.parse(y.created_at as string) - Date.parse(x.created_at as string))[0];
          const segs = segments
            .filter((s) => s.script_id === sc.id)
            .sort((x, y) => (x.segment_index as number) - (y.segment_index as number));
          return send(res, 200, {
            script: sc,
            job: latest ?? null,
            segments: segs.map((seg) => ({
              id: seg.id,
              segment_index: seg.segment_index,
              estimated_duration_ms: seg.estimated_duration_ms,
              audio: segmentAudio.find((au) => au.script_segment_id === seg.id) ?? null,
            })),
          });
        }
      }

      if (method === 'GET' && !a) return send(res, 200, page(mine));
      if (method === 'GET' && a && !b) {
        const sc = mine.find((s) => s.id === a);
        if (!sc) return fail(res, 404, 'not_found', `Script ${a} not found.`);
        return send(res, 200, {
          ...sc,
          segments: segments
            .filter((s) => s.script_id === sc.id)
            .sort((x, y) => (x.segment_index as number) - (y.segment_index as number)),
        });
      }
      if (method === 'POST' && !a) {
        if (
          process.env.MOCK_WITH_CLAIMS !== '1' &&
          !mine.some((s) => s.status === 'approved' || s.status === 'draft') &&
          !claims.some((c) => c.project_id === pid && c.status === 'supported')
        )
          return fail(
            res,
            400,
            'validation_error',
            'This project has no supported claims yet - verify some source material before generating a script.',
          );
        mine
          .filter((s) => ['pending', 'draft', 'approved'].includes(s.status as string))
          .forEach((s) => (s.status = 'superseded'));
        const sc = script(pid, 'pending', null);
        sc.created_at = now();
        job(pid, 'generate_script', 'queued', { payload: { script_id: sc.id, project_id: pid } });
        touch();
        return done(202, sc);
      }
      if (method === 'POST' && a && b === 'approval') {
        const sc = mine.find((s) => s.id === a);
        if (!sc) return fail(res, 404, 'not_found', `Script ${a} not found.`);
        if (sc.status !== 'draft')
          return fail(res, 409, 'conflict', 'Only a draft script can be approved.');
        sc.status = 'approved';
        sc.updated_at = now();
        touch();
        return done(200, sc);
      }
    }
  }

  return fail(res, 404, 'not_found', 'Not found.');
}

if (SCENARIO !== 'empty') seedDefault();

createServer((req, res) => {
  const delay = req.method === 'OPTIONS' ? 0 : LATENCY_MS;
  setTimeout(() => {
    route(req, res).catch((error) => {
      console.error(error);
      fail(res, 500, 'internal_error', 'An unexpected error occurred.');
    });
  }, delay);
}).listen(PORT, () => {
  console.log(
    `Mock EduToon API on http://localhost:${PORT}  (scenario: ${SCENARIO}, latency ${LATENCY_MS} ms)`,
  );
});
