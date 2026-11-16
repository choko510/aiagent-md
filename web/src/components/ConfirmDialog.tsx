import { useEffect, useId, useRef } from "react";

export type ConfirmRequest = {
  title: string;
  summary: string;
  details?: Array<{ label: string; value: string }>;
  warning?: string;
  confirmLabel: string;
  cancelLabel?: string;
  danger?: boolean;
  onConfirm: () => Promise<void> | void;
};

type Props = {
  request: ConfirmRequest | null;
  busy: boolean;
  onClose: () => void;
};

const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

export default function ConfirmDialog({ request, busy, onClose }: Props) {
  const titleId = useId();
  const descId = useId();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const cancelBtnRef = useRef<HTMLButtonElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!request) return;
    previousFocusRef.current = document.activeElement as HTMLElement | null;
    cancelBtnRef.current?.focus();

    function getFocusable(): HTMLElement[] {
      const root = dialogRef.current;
      if (!root) return [];
      return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      );
    }

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape" && !busy) {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key === "Tab") {
        const focusables = getFocusable();
        if (focusables.length === 0) {
          e.preventDefault();
          return;
        }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const active = document.activeElement as HTMLElement | null;
        if (e.shiftKey) {
          if (!active || active === first || !dialogRef.current?.contains(active)) {
            e.preventDefault();
            last.focus();
          }
        } else if (!active || active === last || !dialogRef.current?.contains(active)) {
          e.preventDefault();
          first.focus();
        }
      }
    }

    function onFocusIn(e: FocusEvent) {
      const target = e.target as Node | null;
      if (dialogRef.current && target && !dialogRef.current.contains(target)) {
        const focusables = getFocusable();
        (focusables[0] ?? dialogRef.current).focus();
      }
    }

    window.addEventListener("keydown", onKeyDown);
    document.addEventListener("focusin", onFocusIn);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("focusin", onFocusIn);
      previousFocusRef.current?.focus();
    };
  }, [request, busy, onClose]);

  if (!request) return null;

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div
        ref={dialogRef}
        className="modal-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
        tabIndex={-1}
        data-testid="confirm-dialog"
      >
        <h2 id={titleId}>{request.title}</h2>
        <p id={descId}>{request.summary}</p>
        {request.details && request.details.length > 0 && (
          <dl className="modal-details">
            {request.details.map((d, idx) => (
              <div key={`${d.label}-${idx}`} className="modal-detail-row">
                <dt>{d.label}</dt>
                <dd>{d.value}</dd>
              </div>
            ))}
          </dl>
        )}
        {request.warning && (
          <div className="banner banner-error" role="alert">
            {request.warning}
          </div>
        )}
        <div className="btn-row">
          <button
            ref={cancelBtnRef}
            type="button"
            className="secondary-btn"
            disabled={busy}
            onClick={onClose}
            data-testid="confirm-dialog-cancel"
          >
            {request.cancelLabel ?? "キャンセル"}
          </button>
          <button
            type="button"
            className={request.danger ? "danger-btn" : ""}
            disabled={busy}
            onClick={() => void request.onConfirm()}
            data-testid="confirm-dialog-confirm"
          >
            {request.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
