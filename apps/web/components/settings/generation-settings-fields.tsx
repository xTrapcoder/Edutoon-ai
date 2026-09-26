'use client';

import { Clock, Globe2, MonitorPlay } from 'lucide-react';
import { useEffect, useId, useState } from 'react';
import type { Language, Orientation } from '@edutoon/contracts';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { RadioCard } from '@/components/ui/radio-card';
import { cn } from '@/lib/cn';
import { formatDuration } from '@/lib/format';
import {
  isValidDurationMinutes,
  LANGUAGE_OPTIONS,
  MAX_DURATION_MINUTES,
  MIN_DURATION_MINUTES,
  minutesToMs,
  ORIENTATION_OPTIONS,
} from '@/lib/generation-settings';

export interface GenerationSettingsValue {
  orientation: Orientation;
  durationMinutes: number;
  language: Language;
}

interface Props {
  value: GenerationSettingsValue;
  onChange: (value: GenerationSettingsValue) => void;
  disabled?: boolean;
  /** Frame each group in a card (the wizard) or leave them bare (a dialog). */
  framed?: boolean;
}

function OrientationGlyph({ orientation }: { orientation: Orientation }) {
  return (
    <span
      aria-hidden
      className={cn(
        'block rounded-[3px] border-2 border-brand-600 bg-brand-100',
        orientation === 'horizontal' ? 'h-4 w-7' : 'h-7 w-4',
      )}
    />
  );
}

function DurationControl({
  minutes,
  onChange,
  disabled,
}: {
  minutes: number;
  onChange: (minutes: number) => void;
  disabled?: boolean;
}) {
  const sliderId = useId();
  const numberId = useId();
  const errorId = useId();
  const [text, setText] = useState(String(minutes));

  // Keep the box in step when the slider (or a reset) changes the value.
  useEffect(() => {
    setText(String(minutes));
  }, [minutes]);

  const typed = Number(text);
  const invalid = text.trim() === '' || !isValidDurationMinutes(typed);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <label htmlFor={sliderId} className="sr-only">
            Target duration
          </label>
          <p className="text-sm text-ink-600">
            Choose from {MIN_DURATION_MINUTES} to {MAX_DURATION_MINUTES} minutes.{' '}
            <span className="font-semibold text-ink-950">
              {formatDuration(minutesToMs(minutes))}
            </span>{' '}
            selected.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <label htmlFor={numberId} className="sr-only">
            Target duration in minutes
          </label>
          <input
            id={numberId}
            type="number"
            inputMode="numeric"
            min={MIN_DURATION_MINUTES}
            max={MAX_DURATION_MINUTES}
            step={1}
            value={text}
            disabled={disabled}
            aria-invalid={invalid || undefined}
            aria-describedby={invalid ? errorId : undefined}
            onChange={(event) => {
              setText(event.target.value);
              const next = Number(event.target.value);
              if (event.target.value.trim() !== '' && isValidDurationMinutes(next)) onChange(next);
            }}
            onBlur={() => setText(String(minutes))}
            className="h-10 w-20 rounded-md border border-line-strong bg-surface px-3 text-center text-sm font-semibold text-ink-900 shadow-xs focus:border-brand-500 focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-brand-500/40 aria-[invalid=true]:border-danger-600"
          />
          <span className="text-sm text-ink-600">minutes</span>
        </div>
      </div>
      <input
        id={sliderId}
        type="range"
        min={MIN_DURATION_MINUTES}
        max={MAX_DURATION_MINUTES}
        step={1}
        value={minutes}
        disabled={disabled}
        aria-valuetext={formatDuration(minutesToMs(minutes))}
        onChange={(event) => onChange(Number(event.target.value))}
        className="h-2 w-full cursor-pointer accent-brand-600"
      />
      <div className="flex justify-between text-xs text-ink-500" aria-hidden>
        <span>{MIN_DURATION_MINUTES} min</span>
        <span>{MAX_DURATION_MINUTES} min</span>
      </div>
      {invalid ? (
        <p id={errorId} role="alert" className="text-[13px] font-medium text-danger-700">
          Enter a whole number of minutes from {MIN_DURATION_MINUTES} to {MAX_DURATION_MINUTES}.
        </p>
      ) : null}
    </div>
  );
}

function Group({
  framed,
  title,
  description,
  icon,
  children,
}: {
  framed: boolean;
  title: string;
  description?: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  if (!framed) {
    return (
      <fieldset className="space-y-3">
        <legend className="text-sm font-semibold text-ink-950">{title}</legend>
        {description ? <p className="-mt-1 text-[13px] text-ink-500">{description}</p> : null}
        {children}
      </fieldset>
    );
  }
  return (
    <Card>
      <fieldset>
        <CardHeader
          title={<legend className="contents">{title}</legend>}
          description={description}
          icon={icon}
        />
        <CardBody>{children}</CardBody>
      </fieldset>
    </Card>
  );
}

/** Orientation, target duration and language: the three settings every later stage reads. */
export function GenerationSettingsFields({ value, onChange, disabled, framed = true }: Props) {
  const name = useId();
  return (
    <div className="space-y-6">
      <Group
        framed={framed}
        title="Video orientation"
        description="The shape of the finished video."
        icon={<MonitorPlay className="size-4" aria-hidden />}
      >
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {ORIENTATION_OPTIONS.map((option) => (
            <RadioCard
              key={option.value}
              name={`${name}-orientation`}
              value={option.value}
              checked={value.orientation === option.value}
              disabled={disabled}
              onChange={() => onChange({ ...value, orientation: option.value })}
              icon={<OrientationGlyph orientation={option.value} />}
              title={
                <>
                  {option.label} <span className="font-normal text-ink-500">({option.ratio})</span>
                </>
              }
              description={option.hint}
            />
          ))}
        </div>
      </Group>

      <Group
        framed={framed}
        title="Target duration"
        description="The script is written to about this length."
        icon={<Clock className="size-4" aria-hidden />}
      >
        <DurationControl
          minutes={value.durationMinutes}
          onChange={(durationMinutes) => onChange({ ...value, durationMinutes })}
          disabled={disabled}
        />
      </Group>

      <Group
        framed={framed}
        title="Language"
        description="The language, and writing style, of topics and script."
        icon={<Globe2 className="size-4" aria-hidden />}
      >
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {LANGUAGE_OPTIONS.map((option) => (
            <RadioCard
              key={option.value}
              name={`${name}-language`}
              value={option.value}
              checked={value.language === option.value}
              disabled={disabled}
              onChange={() => onChange({ ...value, language: option.value })}
              title={option.label}
              description={option.hint}
            />
          ))}
        </div>
      </Group>
    </div>
  );
}
