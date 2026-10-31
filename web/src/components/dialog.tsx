"use client";

import { X } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";

/** Native <dialog>: focus trapping, Esc to close and an inert background for free. */
export function Dialog({
  open,
  onClose,
  title,
  children,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      d.showModal();
      d.querySelector<HTMLElement>("[data-autofocus]")?.focus();
    }
    if (!open && d.open) d.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onClick={(e) => e.target === ref.current && onClose()}
      aria-labelledby="dialog-title"
      className="m-auto w-[calc(100%-2rem)] rounded-[24px] border border-line-strong bg-ink-2 p-0 text-text shadow-2xl backdrop:bg-ink/70 backdrop:backdrop-blur-sm"
      style={{ maxWidth: wide ? 640 : 460 }}
    >
      {open ? (
        <div className="p-6">
          <div className="mb-5 flex items-start justify-between gap-3">
            <h2 id="dialog-title" className="text-xl font-semibold">
              {title}
            </h2>
            <button onClick={onClose} aria-label="Close" className="rounded-full p-1 text-muted hover:text-text">
              <X size={18} />
            </button>
          </div>
          {children}
        </div>
      ) : null}
    </dialog>
  );
}
