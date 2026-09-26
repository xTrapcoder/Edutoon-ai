'use client';

import { Plus, X } from 'lucide-react';
import { useId, useState, type KeyboardEvent } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/field';
import { MAX_KEYWORD_CHARS, MAX_KEYWORDS, mergeKeywords } from './wizard-model';

interface KeywordInputProps {
  keywords: string[];
  onChange: (keywords: string[]) => void;
  error?: string | null;
  disabled?: boolean;
  /** Text typed but not yet added is reported here, so a step can add it when the person moves on. */
  onDraftChange?: (draft: string) => void;
}

/** Type a topic and press Enter (or a comma) to add it; paste a list and it is split for you. */
export function KeywordInput({
  keywords,
  onChange,
  error,
  disabled,
  onDraftChange,
}: KeywordInputProps) {
  const inputId = useId();
  const hintId = useId();
  const errorId = useId();
  const [draft, setDraft] = useState('');
  const [notice, setNotice] = useState<string | null>(null);

  const setDraftValue = (value: string) => {
    setDraft(value);
    onDraftChange?.(value);
  };

  const commit = (raw: string) => {
    if (raw.trim().length === 0) return;
    const merged = mergeKeywords(keywords, raw);
    onChange(merged.keywords);
    setDraftValue('');

    const problems: string[] = [];
    if (merged.duplicates > 0) problems.push(`${merged.duplicates} already added`);
    if (merged.tooLong > 0) problems.push(`${merged.tooLong} over ${MAX_KEYWORD_CHARS} characters`);
    if (merged.overLimit > 0)
      problems.push(`${merged.overLimit} over the limit of ${MAX_KEYWORDS}`);
    setNotice(problems.length > 0 ? `Not added: ${problems.join(', ')}.` : null);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter' || event.key === ',') {
      event.preventDefault();
      commit(draft);
    } else if (event.key === 'Backspace' && draft === '' && keywords.length > 0) {
      onChange(keywords.slice(0, -1));
    }
  };

  const atLimit = keywords.length >= MAX_KEYWORDS;

  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        <div className="flex-1">
          <label htmlFor={inputId} className="sr-only">
            Add a keyword
          </label>
          <Input
            id={inputId}
            value={draft}
            disabled={disabled || atLimit}
            placeholder={atLimit ? 'Keyword limit reached' : 'Type a topic, then press Enter'}
            aria-describedby={[hintId, error ? errorId : null].filter(Boolean).join(' ')}
            aria-invalid={error ? true : undefined}
            onChange={(event) => setDraftValue(event.target.value)}
            onKeyDown={onKeyDown}
            onPaste={(event) => {
              const text = event.clipboardData.getData('text');
              if (/[,;\n]/.test(text)) {
                event.preventDefault();
                commit(draft + text);
              }
            }}
          />
        </div>
        <Button
          variant="secondary"
          onClick={() => commit(draft)}
          disabled={disabled || atLimit || draft.trim() === ''}
          leadingIcon={<Plus className="size-4" aria-hidden />}
          aria-label="Add keyword"
        >
          Add
        </Button>
      </div>
      <p id={hintId} className="text-[13px] text-ink-500">
        {keywords.length} of {MAX_KEYWORDS} added. Separate several with commas.
      </p>
      {notice ? (
        <p role="status" className="text-[13px] font-medium text-warning-700">
          {notice}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} role="alert" className="text-[13px] font-medium text-danger-700">
          {error}
        </p>
      ) : null}
      {keywords.length > 0 ? (
        <ul className="flex flex-wrap gap-2" aria-label="Keywords added">
          {keywords.map((keyword) => (
            <li
              key={keyword}
              className="inline-flex max-w-full items-center gap-1 rounded-full bg-brand-50 py-1 pr-1 pl-3 text-sm font-medium text-brand-800"
            >
              <span className="truncate">{keyword}</span>
              <button
                type="button"
                disabled={disabled}
                onClick={() => onChange(keywords.filter((existing) => existing !== keyword))}
                aria-label={`Remove ${keyword}`}
                className="rounded-full p-1 text-brand-600 hover:bg-brand-100 hover:text-brand-900 disabled:opacity-50"
              >
                <X className="size-3.5" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
