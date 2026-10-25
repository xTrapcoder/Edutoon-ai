import type { Metadata } from 'next';
import { VideoPage } from '@/components/workspace/stage-pages';

export const metadata: Metadata = { title: 'Video' };

export default function ProjectVideoPage() {
  return <VideoPage />;
}
