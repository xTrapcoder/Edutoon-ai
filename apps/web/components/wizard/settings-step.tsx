'use client';

import { Info } from 'lucide-react';
import { GenerationSettingsFields } from '@/components/settings/generation-settings-fields';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import type { WizardValues } from './wizard-model';

interface SettingsStepProps {
  values: WizardValues;
  onChange: (patch: Partial<WizardValues>) => void;
  disabled?: boolean;
}

const USES = [
  ['Topic extraction', 'The language sets how topics and their summaries are written.'],
  [
    'Script generation',
    'The target duration and language shape the length and style of the script.',
  ],
  [
    'Video',
    'Orientation is the shape of the video that is rendered. Videos from 1:00 up to 30:00 are supported, at up to 720p.',
  ],
] as const;

export function SettingsStep({ values, onChange, disabled }: SettingsStepProps) {
  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
      <GenerationSettingsFields
        value={{
          orientation: values.orientation,
          durationMs: values.durationMs,
          language: values.language,
        }}
        disabled={disabled}
        onChange={(next) => onChange(next)}
      />
      <aside className="lg:sticky lg:top-24 lg:self-start">
        <Card>
          <CardHeader title="How these settings are used" as="h2" />
          <CardBody className="space-y-4">
            <dl className="space-y-3">
              {USES.map(([term, description]) => (
                <div key={term}>
                  <dt className="text-sm font-semibold text-ink-950">{term}</dt>
                  <dd className="text-[13px] text-ink-500">{description}</dd>
                </div>
              ))}
            </dl>
            <p className="flex gap-2 rounded-md bg-brand-50 p-3 text-[13px] text-ink-600">
              <Info className="mt-0.5 size-4 shrink-0 text-brand-600" aria-hidden />
              You can change these later from the project page.
            </p>
          </CardBody>
        </Card>
      </aside>
    </div>
  );
}
