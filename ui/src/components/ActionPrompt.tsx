import type { RefObject } from "react";
import { DismissReasons } from "./DismissReasons";

/** The reason prompt before a consequential action. A dismissal also needs one of the fixed reasons. */
export function ActionPrompt({
  action,
  label,
  explanation,
  reason,
  onReason,
  reasonCode,
  onReasonCode,
  busy,
  promptRef,
  onSubmit,
  onCancel,
}: {
  action: string;
  label: string;
  explanation: string;
  reason: string;
  onReason: (text: string) => void;
  reasonCode: string | null;
  onReasonCode: (code: string) => void;
  busy: boolean;
  promptRef: RefObject<HTMLTextAreaElement | null>;
  onSubmit: () => void;
  onCancel: () => void;
}) {
  const dismissing = action === "dismiss";
  const needsCode = dismissing && reasonCode === null;
  return (
    <form
      className="prompt"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
    >
      <p>{explanation}</p>
      {dismissing && (
        <DismissReasons value={reasonCode} onChange={onReasonCode} onEscape={onCancel} />
      )}
      <label htmlFor="reason">{dismissing ? "More detail (optional)" : "Reason (optional)"}</label>
      <textarea
        id="reason"
        ref={promptRef}
        value={reason}
        rows={2}
        maxLength={2000}
        onChange={(e) => {
          onReason(e.target.value);
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") onCancel();
        }}
      />
      <div className="row">
        <button
          type="submit"
          className="primary"
          disabled={busy || needsCode}
          aria-describedby={needsCode ? "pick-reason" : undefined}
        >
          Confirm {label.toLowerCase()}
        </button>
        {needsCode && (
          <span id="pick-reason" className="hint">
            Choose a reason first.
          </span>
        )}
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
