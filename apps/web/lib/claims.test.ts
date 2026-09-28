import { describe, expect, it } from 'vitest';
import { claim } from '@/test/fixtures';
import { claimsForTopic, summariseClaimStatuses } from './claims';

describe('summariseClaimStatuses', () => {
  it('counts each verdict out of the total', () => {
    const claims = [
      claim({ status: 'supported' }),
      claim({ status: 'supported' }),
      claim({ status: 'partially_supported' }),
      claim({ status: 'unsupported' }),
      claim({ status: 'contradicted' }),
    ];
    expect(summariseClaimStatuses(claims)).toEqual({
      total: 5,
      supported: 2,
      partiallySupported: 1,
      unsupported: 1,
      contradicted: 1,
    });
  });

  it('is all zero for an empty list', () => {
    expect(summariseClaimStatuses([])).toEqual({
      total: 0,
      supported: 0,
      partiallySupported: 0,
      unsupported: 0,
      contradicted: 0,
    });
  });
});

describe('claimsForTopic', () => {
  it('keeps only the claims extracted for that topic, never a hand-typed one', () => {
    const claims = [
      claim({ id: 'c1', topic_id: 't1' }),
      claim({ id: 'c2', topic_id: 't2' }),
      claim({ id: 'c3', topic_id: null }),
    ];
    expect(claimsForTopic(claims, 't1').map((c) => c.id)).toEqual(['c1']);
  });
});
