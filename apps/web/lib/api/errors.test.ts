import { describe, expect, it } from 'vitest';
import {
  apiErrorFromFailure,
  apiErrorFromResponse,
  describeError,
  safeServerMessage,
} from './errors';

const response = (status: number) => new Response(null, { status });

describe('safeServerMessage', () => {
  it('keeps a plain sentence written for people', () => {
    const sentence = 'Topic extraction is already queued or running for this project.';
    expect(safeServerMessage(sentence)).toBe(sentence);
  });

  it.each([
    'Traceback (most recent call last): File "app/main.py", line 10',
    'sqlalchemy.exc.IntegrityError: duplicate key value violates unique constraint',
    'SELECT * FROM users WHERE id = 1',
    'anthropic.RateLimitError: 429 {"error": {"type": "rate_limit"}}',
    'Invalid api_key sk-abc123',
    'Error at C:\\app\\services\\topics.py',
    'x'.repeat(400),
  ])('never lets technical text through: %s', (technical) => {
    expect(safeServerMessage(technical)).toBeNull();
  });
});

describe('describeError', () => {
  it("shows the API's own words for a conflict", () => {
    const error = apiErrorFromResponse(response(409), {
      error: { code: 'conflict', message: 'Topic extraction is already queued or running.' },
    });
    expect(describeError(error, { action: 'start extraction' }).message).toBe(
      'Topic extraction is already queued or running.',
    );
  });

  it("never shows a server fault's text, whatever it says", () => {
    const error = apiErrorFromResponse(response(500), {
      error: { code: 'internal_error', message: 'psycopg failed: connection refused at 10.0.0.4' },
    });
    const copy = describeError(error, { action: 'load your projects' });
    expect(JSON.stringify(copy)).not.toMatch(/psycopg|10\.0\.0\.4|refused/);
    expect(copy.message).toContain("couldn't load your projects");
  });

  it('gives a 422 a generic sentence rather than validator wording', () => {
    const error = apiErrorFromResponse(response(422), {
      error: {
        code: 'VALIDATION_ERROR',
        message: 'Request validation failed.',
        details: {
          field_errors: [
            { path: 'body.title', message: 'String should have at least 1 character', type: 'x' },
          ],
        },
      },
    });
    expect(describeError(error).message).not.toMatch(/String should/);
    expect(error.fieldErrors).toHaveLength(1);
  });

  it('copes with the default FastAPI body that the OpenAPI document declares', () => {
    const error = apiErrorFromResponse(response(422), {
      detail: [{ loc: ['body'], msg: 'oops', type: 'x' }],
    });
    expect(error.kind).toBe('invalid');
    expect(error.serverMessage).toBeNull();
  });

  it('tells a dropped connection apart, and says it is safe to retry', () => {
    const offline = apiErrorFromFailure(new TypeError('Failed to fetch'));
    expect(offline.kind).toBe('network');
    expect(offline.outcomeUnknown).toBe(true);
    expect(describeError(offline).title).toMatch(/couldn't reach/i);
  });
});

describe('apiErrorFromResponse - what a refusal says about itself', () => {
  const respond = (status: number) => new Response(null, { status });

  it('reads the machine-readable reason and the topics, whatever the sentence is', () => {
    const error = apiErrorFromResponse(respond(400), {
      error: {
        code: 'validation_error',
        message: 'x'.repeat(400), // too long to show: serverMessage is null
        details: { reason: 'no_verified_evidence', topics: ['Crypto', 7, 'Heart valves'] },
      },
    });

    expect(error.serverMessage).toBeNull();
    expect(error.reason).toBe('no_verified_evidence');
    expect(error.topics).toEqual(['Crypto', 'Heart valves']);
  });

  it('has no reason and no topics when the API sent none', () => {
    const error = apiErrorFromResponse(respond(400), {
      error: { code: 'validation_error', message: 'Too many topics.' },
    });

    expect(error.reason).toBeNull();
    expect(error.topics).toEqual([]);
  });
});
