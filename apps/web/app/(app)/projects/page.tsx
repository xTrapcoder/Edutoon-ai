import type { Metadata } from 'next';
import { Suspense } from 'react';
import { ProjectsView } from '@/components/projects/projects-view';

export const metadata: Metadata = { title: 'Projects' };

export default function ProjectsPage() {
  // `useSearchParams` (the top-bar search lands here with ?q=) needs a Suspense boundary.
  return (
    <Suspense>
      <ProjectsView />
    </Suspense>
  );
}
