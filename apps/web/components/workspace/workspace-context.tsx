'use client';

import { createContext, useContext } from 'react';
import type { Project } from '@edutoon/contracts';
import type { Workspace } from '@/lib/api/queries';
import type { PipelineStage } from '@/lib/pipeline';
import type { Workflow } from '@/lib/workflow';

/** The workspace once the project itself has loaded: `project` and `workflow` are certain to exist. */
export interface LoadedWorkspace extends Omit<Workspace, 'project' | 'workflow'> {
  project: Project;
  workflow: Workflow;
  /** The eight pipeline stages, each with the status the API reports for it. */
  pipeline: PipelineStage[];
}

const WorkspaceContext = createContext<LoadedWorkspace | null>(null);

export const WorkspaceProvider = WorkspaceContext.Provider;

export function useWorkspaceContext(): LoadedWorkspace {
  const workspace = useContext(WorkspaceContext);
  if (!workspace) {
    throw new Error('useWorkspaceContext must be used inside a project workspace.');
  }
  return workspace;
}
