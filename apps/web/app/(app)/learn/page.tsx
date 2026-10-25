import type { Metadata } from 'next';
import { MyCoursesView } from '@/components/training/my-courses';

export const metadata: Metadata = { title: 'My learning' };

export default function LearnPage() {
  return <MyCoursesView />;
}
