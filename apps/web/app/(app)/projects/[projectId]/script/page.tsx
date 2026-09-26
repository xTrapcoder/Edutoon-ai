import type { Metadata } from 'next';
import { ScriptPage } from '@/components/script/script-view';

export const metadata: Metadata = { title: 'Script' };

export default function ProjectScriptPage() {
  return <ScriptPage />;
}
