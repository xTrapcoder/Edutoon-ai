import type { Metadata } from 'next';
import { ClaimsPage } from '@/components/workspace/stage-pages';

export const metadata: Metadata = { title: 'Claims' };

export default function ProjectClaimsPage() {
  return <ClaimsPage />;
}
