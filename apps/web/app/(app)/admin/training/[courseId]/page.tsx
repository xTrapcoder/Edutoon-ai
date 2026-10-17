import type { Metadata } from 'next';
import { CourseDetail } from '@/components/training/course-detail';

export const metadata: Metadata = { title: 'Course' };

export default async function CoursePage({ params }: { params: Promise<{ courseId: string }> }) {
  const { courseId } = await params;
  return <CourseDetail courseId={courseId} />;
}
