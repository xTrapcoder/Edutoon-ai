'use client';

import { Clock, Globe2, MonitorPlay } from 'lucide-react';
import { useEffect, useId, useState } from 'react';
import type { Language, Orientation } from '@edutoon/contracts';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { RadioCard } from '@/components/ui/radio-card';
import { cn } from '@/lib/cn';
import { formatTargetDuration } from '@/lib/format';
import {
  durationProblem,
  formatClock,
  isValidDurationMs,
  LANGUAGE_OPTIONS,
  MAX_SUPPORTED_DURATION_MS,
  MIN_DURATION_MS,
  MS_PER_SECOND,
  ORIENTATION_OPTIONS,
  parseClock,
} from '@/lib/generation-settings';

export interface GenerationSettingsValue {
  orientation: Orientation;
  durationMs: number;
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
  durationMs,
  onChange,
  disabled,
}: {
  durationMs: number;
  onChange: (durationMs: number) => void;
  disabled?: boolean;
}) {
  const sliderId = useId();
  const boxId = useId();
  const errorId = useId();
  const [text, setText] = useState(formatClock(durationMs));

  // Keep the box in step when the slider (or a reset) changes the value - but never rewrite what
  // the person is typing: "3" already means 3:00, and turning it into "3:00" under their cursor
  // would break "3:45".
  useEffect(() => {
    setText((current) => (parseClock(current) === durationMs ? current : formatClock(durationMs)));
  }, [durationMs]);

  const typed = parseClock(text);
  const problem = durationProblem(typed);
  const invalid = problem !== null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <label htmlFor={sliderId} className="sr-only">
            Target duration
          </label>
          <p className="text-sm text-ink-600">
            Any length from {formatClock(MIN_DURATION_MS)} to{' '}
            {formatClock(MAX_SUPPORTED_DURATION_MS)}, to the second.{' '}
            <span className="font-semibold text-ink-950">{formatTargetDuration(durationMs)}</span>{' '}
            selected.
          </p>
          <p className="mt-1 text-[13px] text-ink-500">
            Videos longer than {formatClock(MAX_SUPPORTED_DURATION_MS)} are not supported yet.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <label htmlFor={boxId} className="sr-only">
            Target duration, minutes and seconds
          </label>
          <input
            id={boxId}
            type="text"
            inputMode="numeric"
            autoComplete="off"
            placeholder="5:00"
            value={text}
            disabled={disabled}
            aria-invalid={invalid || undefined}
            aria-describedby={invalid ? errorId : undefined}
            onChange={(event) => {
              setText(event.target.value);
              const next = parseClock(event.target.value);
              if (next !== null && durationProblem(next) === null) onChange(next);
            }}
            onBlur={() => setText(formatClock(durationMs))}
            className="h-10 w-24 rounded-md border border-line-strong bg-surface px-3 text-center text-sm font-semibold text-ink-900 shadow-xs focus:border-brand-500 focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-brand-500/40 aria-[invalid=true]:border-danger-600"
          />
          <span className="text-sm text-ink-600">min:sec</span>
        </div>
      </div>
      <input
        id={sliderId}
        type="range"
        min={MIN_DURATION_MS}
        max={MAX_SUPPORTED_DURATION_MS}
        step={MS_PER_SECOND}
        value={Math.min(Math.max(durationMs, MIN_DURATION_MS), MAX_SUPPORTED_DURATION_MS)}
        disabled={disabled}
        aria-valuetext={formatTargetDuration(durationMs)}
        onChange={(event) => onChange(Number(event.target.value))}
        className="h-2 w-full cursor-pointer accent-brand-600"
      />
      <div className="flex justify-between text-xs text-ink-500" aria-hidden>
        <span>{formatClock(MIN_DURATION_MS)}</span>
        <span>{formatClock(MAX_SUPPORTED_DURATION_MS)}</span>
      </div>
      {invalid ? (
        <p id={errorId} role="alert" className="text-[13px] font-medium text-danger-700">
          {problem}
        </p>
      ) : null}
      {!invalid && !isValidDurationMs(durationMs) ? (
        <p role="alert" className="text-[13px] font-medium text-danger-700">
          {durationProblem(durationMs)}
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
          durationMs={value.durationMs}
          onChange={(durationMs) => onChange({ ...value, durationMs })}
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
