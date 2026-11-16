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

export default function ConfirmDialog({ request, busy, onClose }: Props) {
  const titleId = useId();
  const descId = useId();
  const cancelBtnRef = useRef<HTMLButtonElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!request) return;
    previousFocusRef.current = document.activeElement as HTMLElement | null;
    cancelBtnRef.current?.focus();

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape" && !busy) {
        e.preventDefault();
        onClose();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
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
        className="modal-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
        data-testid="confirm-dialog"
      >
        <h2 id={titleId}>{request.title}</h2>
        <p id={descId}>{request.summary}</p>
        {request.details && request.details.length > 0 && (
          <dl className="modal-details">
            {request.details.map((d) => (
              <div key={d.label} className="modal-detail-row">
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
