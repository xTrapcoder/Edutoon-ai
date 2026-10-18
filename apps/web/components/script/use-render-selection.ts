import { useEffect, useState } from 'react';
import type { Project, ScriptDetail } from '@edutoon/contracts';
import { useRenderStatus } from '@/lib/api/queries';
import type { RenderSelection } from '@/lib/render';

/**
 * Which video a render card is talking about: shape and quality.
 *
 * It starts on the quick preview, but when a finished *Final* video already exists it starts there
 * instead, until the person picks a quality themselves - otherwise a reload opens on the preview and
 * the finished Final video looks as if it had gone missing.
 */
export function useRenderSelection(
  project: Project,
  script: ScriptDetail,
): [RenderSelection, (selection: RenderSelection) => void] {
  const [selection, setSelection] = useState<RenderSelection>({
    orientation: project.orientation,
    quality: 'low',
  });
  const [chosen, setChosen] = useState(false);
  const finalRender = useRenderStatus(
    project.id,
    script.status === 'approved' ? script.id : undefined,
    selection.orientation,
    'high',
  );
  const hasFinalVideo = finalRender.data?.output != null;

  useEffect(() => {
    if (!chosen && hasFinalVideo) {
      setSelection((current) =>
        current.quality === 'high' ? current : { ...current, quality: 'high' },
      );
    }
  }, [chosen, hasFinalVideo]);

  const choose = (next: RenderSelection) => {
    setChosen(true);
    setSelection(next);
  };
  return [selection, choose];
}
