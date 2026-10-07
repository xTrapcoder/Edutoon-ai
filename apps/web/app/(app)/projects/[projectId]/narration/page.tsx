import type { Metadata } from 'next';
import { NarrationPage } from '@/components/workspace/stage-pages';

export const metadata: Metadata = { title: 'Narration' };

export default function ProjectNarrationPage() {
  return <NarrationPage />;
}
