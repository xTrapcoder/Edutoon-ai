import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { PROJECT_ID, topic } from '@/test/fixtures';
import { renderWithClient } from '@/test/render';
import { API, server } from '@/test/server';
import { KeywordCoverageCard } from './keyword-coverage';

const item = (over: Record<string, unknown>) => ({
  topic_id: '00000000-0000-4000-8000-000000000001',
  title: 'Calvin cycle',
  state: 'covered',
  supported_claims: 3,
  total_claims: 3,
  in_script: null,
  message: 'Covered by 3 statements checked against your document.',
  ...over,
});

const serve = (keywords: unknown[], summary: string | null, complete: boolean) =>
  server.use(
    http.get(`${API}/v1/projects/${PROJECT_ID}/topics/coverage`, () =>
      HttpResponse.json({ keywords, summary, complete }),
    ),
  );

describe('Keyword coverage', () => {
  it('says which keywords the document backs and which it does not - never calling a gap covered', async () => {
    serve(
      [
        item({}),
        item({
          topic_id: '00000000-0000-4000-8000-000000000002',
          title: 'Quantum computing',
          state: 'not_in_document',
          supported_claims: 0,
          total_claims: 4,
          message:
            'Your document does not support this keyword: none of the 4 statements drafted about it could be found in the document.',
        }),
      ],
      '1 of 2 keywords covered by your document; not in your document: Quantum computing.',
      false,
    );
    renderWithClient(<KeywordCoverageCard projectId={PROJECT_ID} />);

    const list = await screen.findByRole('list', { name: 'Keyword coverage' });
    const rows = within(list).getAllByRole('listitem');
    expect(rows[0]).toHaveTextContent('Calvin cycle');
    expect(rows[0]).toHaveTextContent('Covered by your document');
    expect(rows[1]).toHaveTextContent('Quantum computing');
    expect(rows[1]).toHaveTextContent('Not in your document');
    expect(screen.getByText('Some keywords are not in your document')).toBeInTheDocument();
    expect(screen.getByText(/Nothing was invented to fill the gap/)).toBeInTheDocument();
    // Only a keyword the document lacks can be left out from here.
    expect(within(rows[0]!).queryByRole('button')).not.toBeInTheDocument();
    expect(within(rows[1]!).getByRole('button', { name: 'Leave it out' })).toBeInTheDocument();
  });

  it('leaves a keyword out by rejecting its topic', async () => {
    const id = '00000000-0000-4000-8000-000000000002';
    serve(
      [item({ topic_id: id, title: 'Quantum computing', state: 'not_in_document' })],
      'summary',
      false,
    );
    let patched: unknown = null;
    server.use(
      http.patch(`${API}/v1/projects/${PROJECT_ID}/topics/${id}`, async ({ request }) => {
        patched = await request.json();
        return HttpResponse.json(topic({ id, status: 'rejected', origin: 'user' }));
      }),
      http.get(`${API}/v1/projects/${PROJECT_ID}/topics`, () => HttpResponse.json({ items: [] })),
    );
    renderWithClient(<KeywordCoverageCard projectId={PROJECT_ID} />);

    await userEvent.click(await screen.findByRole('button', { name: 'Leave it out' }));

    await screen.findByRole('list', { name: 'Keyword coverage' });
    expect(patched).toEqual({ status: 'rejected' });
  });

  it('shows nothing when the person typed no keywords', async () => {
    serve([], null, true);
    const { container } = renderWithClient(<KeywordCoverageCard projectId={PROJECT_ID} />);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(container).toBeEmptyDOMElement();
  });
});
