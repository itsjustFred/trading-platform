import { memo } from "react";

export const LoadingSpinner = memo(function LoadingSpinner() {
  return (
    <div
      style={{
        minHeight: "40vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: "var(--color-text-muted)",
      }}
    >
      <div style={{ display: "inline-flex", alignItems: "center", gap: "10px" }}>
        <span
          className="animate-spin"
          style={{
            width: "16px",
            height: "16px",
            borderRadius: "999px",
            border: "2px solid rgba(148, 163, 184, 0.35)",
            borderTopColor: "#58a6ff",
            display: "inline-block",
          }}
        />
        <span style={{ fontSize: "0.86rem" }}>Loading...</span>
      </div>
    </div>
  );
});
