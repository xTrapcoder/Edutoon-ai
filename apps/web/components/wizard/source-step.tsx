'use client';

import { ClipboardPaste, FilePlus2, FileText, ListChecks, Presentation } from 'lucide-react';
import { useId } from 'react';
import { Badge } from '@/components/ui/badge';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Field, Input, Textarea } from '@/components/ui/field';
import { RadioCard } from '@/components/ui/radio-card';
import { FileDropzone } from './file-dropzone';
import { KeywordInput } from './keyword-input';
import {
  fileKindOf,
  isModeAvailable,
  KEYWORD_ONLY_UNSUPPORTED_MESSAGE,
  MAX_TEXT_CHARS,
  MAX_TITLE_CHARS,
  MIN_TEXT_CHARS,
  suggestTitle,
  usesDeck,
  usesFile,
  usesKeywords,
  usesText,
  type SourceErrors,
  type SourceMode,
  type WizardValues,
} from './wizard-model';

interface SourceStepProps {
  values: WizardValues;
  errors: SourceErrors;
  disabled?: boolean;
  onChange: (patch: Partial<WizardValues>) => void;
  onTitleEdited: () => void;
  titleTouched: boolean;
  onKeywordDraftChange: (draft: string) => void;
}

const MODES: readonly {
  value: SourceMode;
  title: string;
  description: string;
  icon: typeof FileText;
}[] = [
  {
    value: 'pdf',
    title: 'PDF document',
    description: 'Upload your study material',
    icon: FileText,
  },
  {
    value: 'pptx',
    title: 'PowerPoint deck',
    description: 'Upload a .pptx - explained as one continuous lesson',
    icon: Presentation,
  },
  {
    value: 'text',
    title: 'Paste text',
    description: 'Paste the material to explain - every claim is checked against it',
    icon: ClipboardPaste,
  },
  {
    value: 'keywords',
    title: 'Keywords',
    description: 'Not available yet - paste text, or add keywords to a PDF',
    icon: ListChecks,
  },
  {
    value: 'both',
    title: 'PDF + Keywords',
    description: 'Upload a PDF and add your own topics',
    icon: FilePlus2,
  },
];

export function SourceStep({
  values,
  errors,
  disabled,
  onChange,
  onTitleEdited,
  titleTouched,
  onKeywordDraftChange,
}: SourceStepProps) {
  const name = useId();

  return (
    <div className="space-y-6">
      <Card>
        <CardBody>
          <Field
            label="Project title"
            required
            hint="A name you will recognise later, such as the chapter or course."
            error={errors.title}
          >
            {(control) => (
              <Input
                {...control}
                value={values.title}
                maxLength={MAX_TITLE_CHARS}
                disabled={disabled}
                placeholder="For example: Computer Networks Fundamentals"
                autoComplete="off"
                onChange={(event) => {
                  onTitleEdited();
                  onChange({ title: event.target.value });
                }}
              />
            )}
          </Field>
        </CardBody>
      </Card>

      <Card>
        <fieldset disabled={disabled}>
          <CardHeader
            title={<legend className="contents">Choose your content source</legend>}
            description="What should the video be about?"
          />
          <CardBody className="space-y-6">
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {MODES.map((mode) => (
                <RadioCard
                  key={mode.value}
                  name={`${name}-mode`}
                  value={mode.value}
                  checked={values.mode === mode.value}
                  disabled={!isModeAvailable(mode.value)}
                  onChange={() => onChange({ mode: mode.value })}
                  icon={<mode.icon className="size-5" aria-hidden />}
                  title={mode.title}
                  badge={
                    isModeAvailable(mode.value) ? undefined : <Badge tone="warning">Not yet</Badge>
                  }
                  description={mode.description}
                />
              ))}
            </div>
            <p className="text-[13px] text-ink-500">
              Keyword-only projects are not supported yet: every claim in a video is checked against
              your own source material, and keywords alone give nothing to check against. Upload a
              PDF or a PowerPoint deck, or paste your own text; with a PDF you can also add keywords
              to steer which topics it covers.
            </p>
            {errors.mode ? (
              <p role="alert" className="text-[13px] font-medium text-danger-700">
                {errors.mode || KEYWORD_ONLY_UNSUPPORTED_MESSAGE}
              </p>
            ) : null}

            <div
              className={
                usesFile(values.mode) && usesKeywords(values.mode)
                  ? 'grid gap-8 lg:grid-cols-2'
                  : ''
              }
            >
              {usesFile(values.mode) ? (
                <section aria-labelledby={`${name}-pdf`} className="space-y-3">
                  <h3 id={`${name}-pdf`} className="text-sm font-semibold text-ink-950">
                    {usesDeck(values.mode) ? 'Upload PowerPoint deck' : 'Upload PDF document'}
                  </h3>
                  {usesDeck(values.mode) ? (
                    <p className="text-[13px] text-ink-500">
                      Each slide is read as it is - title, bullets, tables and speaker notes - and
                      explained as one connected lesson, in the order of your deck.
                    </p>
                  ) : null}
                  <FileDropzone
                    kind={fileKindOf(values.mode)}
                    file={values.file}
                    error={errors.file}
                    disabled={disabled}
                    onChange={(file) => {
                      onChange({
                        file,
                        ...(file && !titleTouched && values.title.trim() === ''
                          ? { title: suggestTitle(file.name) }
                          : {}),
                      });
                    }}
                  />
                </section>
              ) : null}

              {usesText(values.mode) ? (
                <section aria-labelledby={`${name}-text`} className="space-y-3">
                  <h3 id={`${name}-text`} className="text-sm font-semibold text-ink-950">
                    Paste your material
                  </h3>
                  <p className="text-[13px] text-ink-500">
                    Paste the text the video should be about - notes, an article, a chapter. Every
                    claim in the video is checked against it, and nothing is added from outside. A
                    topic name on its own is not enough: there would be nothing to check against.
                  </p>
                  <Field
                    label="Your text"
                    required
                    error={errors.text}
                    hint={`${values.text.trim().length.toLocaleString('en-GB')} characters - at least ${MIN_TEXT_CHARS}, at most ${MAX_TEXT_CHARS.toLocaleString('en-GB')}.`}
                  >
                    {(control) => (
                      <Textarea
                        {...control}
                        rows={12}
                        value={values.text}
                        disabled={disabled}
                        placeholder="Paste or type the material here"
                        onChange={(event) => onChange({ text: event.target.value })}
                      />
                    )}
                  </Field>
                </section>
              ) : null}

              {usesKeywords(values.mode) ? (
                <section aria-labelledby={`${name}-keywords`} className="space-y-3">
                  <h3 id={`${name}-keywords`} className="text-sm font-semibold text-ink-950">
                    {values.mode === 'both' ? 'Add keywords' : 'Your keywords'}
                  </h3>
                  <p className="text-[13px] text-ink-500">
                    {values.mode === 'both'
                      ? 'Topics you definitely want covered. They are kept as they are, alongside what we find in the PDF.'
                      : 'The topics you want covered. We can suggest related subtopics, which are clearly marked as not coming from a source.'}
                  </p>
                  <KeywordInput
                    keywords={values.keywords}
                    onChange={(keywords) => onChange({ keywords })}
                    error={errors.keywords}
                    disabled={disabled}
                    onDraftChange={onKeywordDraftChange}
                  />
                </section>
              ) : null}
            </div>
          </CardBody>
        </fieldset>
      </Card>
    </div>
  );
}
