import type { Metadata } from 'next';
import { TopicsPage } from '@/components/topics/topics-view';

export const metadata: Metadata = { title: 'Topics' };

export default function ProjectTopicsPage() {
  return <TopicsPage />;
}
