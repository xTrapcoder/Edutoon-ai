/**
 * API contracts for EduToon AI.
 *
 * Everything here is derived from `openapi.json`, which `apps/api/scripts/export_openapi.py`
 * writes from the FastAPI application. Regenerate with `make contracts`; never edit
 * `src/schema.d.ts` by hand and never re-declare an API shape elsewhere - alias it from here.
 */
import type { components } from './src/schema';

export type { components, operations, paths } from './src/schema';

type Schemas = components['schemas'];

export type Project = Schemas['ProjectResponse'];
export type ProjectCreateRequest = Schemas['ProjectCreateRequest'];
export type ProjectUpdateRequest = Schemas['ProjectUpdateRequest'];
export type ProjectList = Schemas['ProjectListResponse'];

export type Source = Schemas['UploadedSourceResponse'];
export type SourceChunk = Schemas['SourceChunkResponse'];

export type Topic = Schemas['TopicResponse'];
export type TopicEvidence = Schemas['TopicEvidenceResponse'];
export type TopicUpdateRequest = Schemas['TopicUpdateRequest'];

export type Job = Schemas['JobResponse'];

export type Script = Schemas['ScriptResponse'];
export type ScriptDetail = Schemas['ScriptDetailResponse'];
export type ScriptSegment = Schemas['ScriptSegmentResponse'];
export type CitedClaim = Schemas['CitedClaimResponse'];

export type SynthesisStatus = Schemas['SynthesisStatusResponse'];
export type SynthesisSegment = Schemas['SynthesisSegmentResponse'];
export type SegmentAudio = Schemas['SegmentAudioResponse'];

export type ClaimVerification = Schemas['ClaimVerificationResponse'];

/** The vocabularies below are the API's own `Literal` types, read back out of the schema. */
export type Orientation = Project['orientation'];
export type Language = Project['language'];
export type ProjectSourceType = Project['source_type'];
export type TopicStatus = Topic['status'];
export type TopicOrigin = Topic['origin'];
export type ScriptStatus = Script['status'];
export type JobKind = Job['kind'];
export type JobStatus = Job['status'];
export type SourceStatus = Source['status'];
export type VerdictStatus = CitedClaim['status'];
export type AudioStatus = SegmentAudio['status'];
