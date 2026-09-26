import type { Metadata } from 'next';
import { ProjectOverview } from '@/components/workspace/overview';

export const metadata: Metadata = { title: 'Project' };

export default function ProjectPage() {
  return <ProjectOverview />;
}
