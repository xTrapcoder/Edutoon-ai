import type { Metadata } from 'next';
import { ModuleView } from '@/components/training/module-view';

export const metadata: Metadata = { title: 'Module' };

export default async function ModulePage({ params }: { params: Promise<{ moduleId: string }> }) {
  const { moduleId } = await params;
  return <ModuleView moduleId={moduleId} />;
}
