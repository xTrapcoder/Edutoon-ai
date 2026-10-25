'use client';

import { Download } from 'lucide-react';
import { useEffect, useState } from 'react';
import { fetchSegmentAudioUrl } from '@/lib/api/endpoints';

interface SegmentAudioPlayerProps {
  projectId: string;
  scriptId: string;
  segmentId: string;
  /** When given, a download link for the audio is offered under the player, saved under this name. */
  downloadName?: string;
}

/**
 * One segment's narration, played from a `blob:` URL fetched with the caller's own auth token
 * (see `fetchSegmentAudioUrl` - a plain `<audio src>` cannot carry an `Authorization` header).
 * Re-fetches whenever the segment changes, and always revokes the URL it created.
 */
export function SegmentAudioPlayer({
  projectId,
  scriptId,
  segmentId,
  downloadName,
}: SegmentAudioPlayerProps) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    setUrl(null);
    setFailed(false);

    fetchSegmentAudioUrl(projectId, scriptId, segmentId)
      .then((fetched) => {
        if (cancelled) {
          URL.revokeObjectURL(fetched);
          return;
        }
        objectUrl = fetched;
        setUrl(fetched);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [projectId, scriptId, segmentId]);

  if (failed) {
    return <p className="text-[13px] text-danger-600">This narration couldn&apos;t be loaded.</p>;
  }
  if (!url) {
    return <p className="text-[13px] text-ink-500">Loading narration…</p>;
  }
  return (
    <div className="space-y-1.5">
      <audio controls src={url} className="w-full" preload="metadata">
        Your browser does not support the audio element.
      </audio>
      {downloadName ? (
        <a
          href={url}
          download={downloadName}
          className="inline-flex items-center gap-1.5 text-[13px] font-medium text-brand-700 hover:underline"
        >
          <Download className="size-3.5" aria-hidden />
          Download audio
        </a>
      ) : null}
    </div>
  );
}
