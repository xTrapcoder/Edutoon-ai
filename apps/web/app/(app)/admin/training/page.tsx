import type { Metadata } from 'next';
import { CoursesView } from '@/components/training/courses-view';

export const metadata: Metadata = { title: 'Training' };

export default function TrainingPage() {
  return <CoursesView />;
}
