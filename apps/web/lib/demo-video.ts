import { statSync } from 'node:fs';
import path from 'node:path';

/**
 * The two locally verified renders the Video Demo page plays. Server-side only (it reads the disk).
 *
 * They are the existing output of `apps/api/scripts/verify_phase11.py`, which drives the real local
 * pipeline - Piper narration, the Pillow scene compositor and FFmpeg - from a verification script.
 * The one stand-in is the AI storyboard composer. They are not made from an uploaded PDF, and nothing
 * here generates or fakes a video: the files are served exactly as they sit in
 * `apps/api/var/phase11-verification/` (git-ignored, so present only where that script has been run).
 *
 * The facts below were read from the files with `ffprobe` on 30 September 2026.
 */
export const DEMO_VIDEO_DIR = path.resolve(process.cwd(), '../api/var/phase11-verification');

export type DemoVideoKey = 'horizontal' | 'vertical';

export interface DemoVideo {
  key: DemoVideoKey;
  file: string;
  title: string;
  ratio: string;
  frame: string;
  duration: string;
}

export const DEMO_VIDEOS: Record<DemoVideoKey, DemoVideo> = {
  horizontal: {
    key: 'horizontal',
    file: 'horizontal-low.mp4',
    title: 'Horizontal Demo',
    ratio: '16:9',
    frame: '960 × 540',
    duration: '2 min 24 s',
  },
  vertical: {
    key: 'vertical',
    file: 'vertical-low.mp4',
    title: 'Vertical Demo',
    ratio: '9:16',
    frame: '540 × 960',
    duration: '2 min 24 s',
  },
};

export function isDemoVideoKey(value: string): value is DemoVideoKey {
  return value === 'horizontal' || value === 'vertical';
}

/** The absolute path of a demo video. Only the two fixed names above can ever be resolved. */
export function demoVideoPath(key: DemoVideoKey): string {
  return path.join(DEMO_VIDEO_DIR, DEMO_VIDEOS[key].file);
}

/** The file's size in bytes, or `null` if it is not on this machine. */
export function demoVideoSize(key: DemoVideoKey): number | null {
  try {
    return statSync(demoVideoPath(key)).size;
  } catch {
    return null;
  }
}
