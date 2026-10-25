import { screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { job } from '@/test/fixtures';
import { renderWithClient } from '@/test/render';
import { API, server } from '@/test/server';
import { JobProgressCard, stepsForJob } from './job-progress';

const labels = { ready: 'Your PDF was read', working: 'Finding topics' };

function card(status: 'queued' | 'running') {
  const now = new Date().toISOString();
  const j = job({ status, kind: 'extract_topics', created_at: now, started_at: now });
  return (
    <JobProgressCard
      icon={null}
      title="Finding topics"
      description="d"
      job={j}
      steps={stepsForJob(j, labels)}
    />
  );
}

describe('JobProgressCard and a missing worker', () => {
  it('says plainly that nothing is processing when the worker is offline', async () => {
    server.use(
      http.get(`${API}/health`, () => HttpResponse.json({ status: 'ok', worker: 'offline' })),
    );
    renderWithClient(card('queued'));
    expect(await screen.findByText(/background worker is not running/i)).toBeInTheDocument();
  });

  it('stays quiet when the worker is running', async () => {
    server.use(http.get(`${API}/health`, () => HttpResponse.json({ status: 'ok', worker: 'ok' })));
    renderWithClient(card('queued'));
    expect(await screen.findByText(/You can leave this page/i)).toBeInTheDocument();
    expect(screen.queryByText(/background worker is not running/i)).not.toBeInTheDocument();
  });

  it('does not ask, or warn, about a job that is already running', async () => {
    let asked = false;
    server.use(
      http.get(`${API}/health`, () => {
        asked = true;
        return HttpResponse.json({ status: 'ok', worker: 'offline' });
      }),
    );
    renderWithClient(card('running'));
    expect(await screen.findByText(/You can leave this page/i)).toBeInTheDocument();
    expect(asked).toBe(false);
  });
});
