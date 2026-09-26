'use client';

import { useEffect, useState } from 'react';

/** The current time in milliseconds, refreshed every `intervalMs` - for elapsed-time displays. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}
