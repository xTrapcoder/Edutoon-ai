import type { Metadata } from 'next';
import { StoryboardPage } from '@/components/workspace/stage-pages';

export const metadata: Metadata = { title: 'Storyboard' };

export default function ProjectStoryboardPage() {
  return <StoryboardPage />;
}
