import { createReadStream } from 'node:fs';
import { Readable } from 'node:stream';
import { demoVideoPath, demoVideoSize, isDemoVideoKey } from '@/lib/demo-video';

// Streams one of the two existing, locally verified renders (see `lib/demo-video.ts`) with `Range`
// support, so the browser's player can seek. It serves those two fixed files and nothing else.
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const HEADERS = {
  'Content-Type': 'video/mp4',
  'Accept-Ranges': 'bytes',
  'Cache-Control': 'no-cache',
};

export async function GET(request: Request, { params }: { params: Promise<{ name: string }> }) {
  const { name } = await params;
  if (!isDemoVideoKey(name)) return new Response('Not found', { status: 404 });

  const size = demoVideoSize(name);
  if (size === null) {
    return new Response('The verified render is not on this machine.', { status: 404 });
  }

  let start = 0;
  let end = size - 1;
  let status = 200;

  const range = request.headers.get('range');
  if (range) {
    const match = /^bytes=(\d*)-(\d*)$/.exec(range.trim());
    if (!match || (match[1] === '' && match[2] === '')) {
      return new Response(null, {
        status: 416,
        headers: { ...HEADERS, 'Content-Range': `bytes */${size}` },
      });
    }
    if (match[1] === '') {
      // "the last N bytes"
      start = Math.max(0, size - Number(match[2]));
    } else {
      start = Number(match[1]);
      if (match[2] !== '') end = Math.min(Number(match[2]), size - 1);
    }
    if (start >= size || start > end) {
      return new Response(null, {
        status: 416,
        headers: { ...HEADERS, 'Content-Range': `bytes */${size}` },
      });
    }
    status = 206;
  }

  const stream = Readable.toWeb(
    createReadStream(demoVideoPath(name), { start, end }),
  ) as unknown as ReadableStream<Uint8Array>;

  return new Response(stream, {
    status,
    headers: {
      ...HEADERS,
      'Content-Length': String(end - start + 1),
      ...(status === 206 ? { 'Content-Range': `bytes ${start}-${end}/${size}` } : {}),
    },
  });
}
