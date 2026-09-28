'use client';

import { useEffect, useState } from 'react';
import { fetchSegmentAudioUrl } from '@/lib/api/endpoints';

interface SegmentAudioPlayerProps {
  projectId: string;
  scriptId: string;
  segmentId: string;
}

/**
 * One segment's narration, played from a `blob:` URL fetched with the caller's own auth token
 * (see `fetchSegmentAudioUrl` - a plain `<audio src>` cannot carry an `Authorization` header).
 * Re-fetches whenever the segment changes, and always revokes the URL it created.
 */
export function SegmentAudioPlayer({ projectId, scriptId, segmentId }: SegmentAudioPlayerProps) {
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
    <audio controls src={url} className="w-full" preload="metadata">
      Your browser does not support the audio element.
    </audio>
  );
}
