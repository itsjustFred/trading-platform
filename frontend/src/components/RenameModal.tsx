import { useState } from "react";
import { Loader2, PencilLine } from "lucide-react";
import { Modal } from "./Modal";

interface RenameModalProps {
  open: boolean;
  title: string;
  label: string;
  value: string;
  helperText?: string;
  saving?: boolean;
  onClose: () => void;
  onSave: (value: string) => Promise<void> | void;
}

export function RenameModal({
  open,
  title,
  label,
  value,
  helperText,
  saving = false,
  onClose,
  onSave,
}: Readonly<RenameModalProps>) {
  return (
    <Modal open={open} onClose={onClose} title={title} width="420px">
      {/* Mounted only while open so the draft always starts from `value`. */}
      {open ? (
        <RenameForm
          label={label}
          value={value}
          helperText={helperText}
          saving={saving}
          onClose={onClose}
          onSave={onSave}
        />
      ) : null}
    </Modal>
  );
}

function RenameForm({
  label,
  value,
  helperText,
  saving,
  onClose,
  onSave,
}: Readonly<
  Omit<RenameModalProps, "open" | "title">
>) {
  const [draft, setDraft] = useState(value);

  const trimmed = draft.trim();

  return (
    <div style={{ display: "grid", gap: "14px" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "10px",
          padding: "10px 12px",
          borderRadius: "10px",
          background:
            "linear-gradient(135deg, rgba(88,166,255,0.14), rgba(99,102,241,0.08))",
          border: "1px solid rgba(88,166,255,0.18)",
          color: "var(--color-text-muted)",
          fontSize: "0.8rem",
          lineHeight: 1.5,
        }}
      >
        <PencilLine size={16} color="#58a6ff" />
        Use a clear, human-readable title. The strategy slug or backtest ID stays unchanged.
      </div>

      <div style={{ display: "grid", gap: "6px" }}>
        <label
          style={{
            color: "var(--color-text-muted)",
            fontSize: "0.75rem",
            textTransform: "uppercase",
            letterSpacing: "0.08em",
          }}
        >
          {label}
        </label>
        <input
          type="text"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={value}
          autoFocus
          style={{
            background: "var(--color-surface2)",
            border: "1px solid var(--color-border)",
            color: "var(--color-text)",
            padding: "10px 12px",
            borderRadius: "8px",
            fontSize: "0.9rem",
            outline: "none",
          }}
        />
        {helperText ? (
          <div style={{ color: "var(--color-text-muted)", fontSize: "0.76rem" }}>
            {helperText}
          </div>
        ) : null}
      </div>

      <div style={{ display: "flex", justifyContent: "flex-end", gap: "10px" }}>
        <button
          type="button"
          onClick={onClose}
          disabled={saving}
          style={{
            background: "transparent",
            color: "var(--color-text-muted)",
            border: "1px solid var(--color-border)",
            borderRadius: "8px",
            padding: "9px 14px",
            cursor: saving ? "not-allowed" : "pointer",
          }}
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={() => onSave(trimmed)}
          disabled={saving || !trimmed}
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "8px",
            background: trimmed
              ? "linear-gradient(135deg, #58a6ff, #7c3aed)"
              : "var(--color-surface2)",
            color: trimmed ? "#fff" : "var(--color-text-muted)",
            border: "none",
            borderRadius: "8px",
            padding: "9px 14px",
            minWidth: "120px",
            cursor: saving || !trimmed ? "not-allowed" : "pointer",
          }}
        >
          {saving ? <Loader2 size={14} className="animate-spin" /> : null}
          Save
        </button>
      </div>
    </div>
  );
}
