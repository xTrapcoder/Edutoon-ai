'use client';

import { X } from 'lucide-react';
import { useEffect, useId, useRef, type ReactNode } from 'react';
import { cn } from '@/lib/cn';

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  /** Buttons for the bottom of the dialog. */
  footer?: ReactNode;
  size?: 'sm' | 'md' | 'lg';
  /** Stop Escape / backdrop clicks closing the dialog (while a save is in flight). */
  dismissible?: boolean;
}

const widths = { sm: 'max-w-md', md: 'max-w-xl', lg: 'max-w-3xl' } as const;

/**
 * A modal dialog on the platform's `<dialog>`: it traps focus, closes on Escape, marks the rest
 * of the page inert and returns focus to whatever opened it - all without extra code.
 */
export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = 'md',
  dismissible = true,
}: ModalProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined}
      onCancel={(event) => {
        event.preventDefault();
        if (dismissible) onClose();
      }}
      onClick={(event) => {
        // A click on the backdrop lands on the <dialog> itself, not on anything inside it.
        if (dismissible && event.target === ref.current) onClose();
      }}
      className={cn(
        'm-auto w-[calc(100%-32px)] rounded-xl border border-line bg-surface p-0 text-ink-700 shadow-pop backdrop:bg-ink-950/45 backdrop:backdrop-blur-[2px] open:animate-rise',
        widths[size],
      )}
    >
      {open ? (
        <div className="flex max-h-[85vh] flex-col">
          <div className="flex items-start justify-between gap-4 px-6 pt-6">
            <div className="min-w-0">
              <h2 id={titleId} className="text-lg font-semibold text-ink-950">
                {title}
              </h2>
              {description ? (
                <p id={descriptionId} className="mt-1 text-sm text-ink-500">
                  {description}
                </p>
              ) : null}
            </div>
            {dismissible ? (
              <button
                type="button"
                onClick={onClose}
                aria-label="Close"
                className="-mt-1 -mr-2 rounded-md p-2 text-ink-500 hover:bg-brand-50 hover:text-ink-900"
              >
                <X className="size-5" aria-hidden />
              </button>
            ) : null}
          </div>
          {children ? <div className="overflow-y-auto px-6 py-5">{children}</div> : null}
          {footer ? (
            <div className="flex flex-wrap items-center justify-end gap-3 border-t border-line px-6 py-4">
              {footer}
            </div>
          ) : null}
        </div>
      ) : null}
    </dialog>
  );
}
