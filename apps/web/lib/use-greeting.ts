'use client';

import { useEffect, useState } from 'react';
import { greetingFor } from '@/lib/format';

/**
 * A greeting for the visitor's local time of day. It starts neutral and settles after mount, so
 * the server-rendered page and the first client render always match.
 */
export function useGreeting(): string {
  const [greeting, setGreeting] = useState('Welcome back');
  useEffect(() => {
    setGreeting(greetingFor(new Date()));
  }, []);
  return greeting;
}
