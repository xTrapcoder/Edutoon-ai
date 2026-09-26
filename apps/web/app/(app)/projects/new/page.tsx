import type { Metadata } from 'next';
import { CreateProjectWizard } from '@/components/wizard/create-project-wizard';

export const metadata: Metadata = { title: 'Create project' };

export default function NewProjectPage() {
  return <CreateProjectWizard />;
}
