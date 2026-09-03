import { Component, type ErrorInfo, type ReactNode } from "react";

type ErrorBoundaryProps = {
  children: ReactNode;
  /** Optional label rendered in the fallback to locate the failing subtree. */
  label?: string;
};

type ErrorBoundaryState = {
  error: Error | null;
};

/**
 * Application-level guard against render-time throws (optional-field derefs,
 * backend shape drift) and failed lazy-chunk loads. Without it a single throw
 * anywhere in the tree whitescreens the SPA; with it the failure is contained
 * to the wrapped subtree and recoverable via the reset button.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Keep a console trace for debugging until a reporting sink (e.g. Sentry) is wired.
    console.error("[ErrorBoundary] caught render error:", error, info.componentStack);
  }

  private handleReset = () => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) {
      return this.props.children;
    }

    return (
      <div
        role="alert"
        style={{
          margin: "24px",
          padding: "20px",
          border: "1px solid rgba(248, 113, 113, 0.4)",
          borderRadius: "12px",
          background: "rgba(248, 113, 113, 0.08)",
          color: "var(--color-text, #e6edf3)",
        }}
      >
        <h2 style={{ margin: "0 0 8px", fontSize: "1.05rem", fontWeight: 700 }}>
          {this.props.label ? `${this.props.label} failed to render` : "Something went wrong"}
        </h2>
        <p style={{ margin: "0 0 12px", fontSize: "0.85rem", color: "var(--color-text-muted, #8b949e)" }}>
          {error.message || "An unexpected rendering error occurred."}
        </p>
        <button
          type="button"
          onClick={this.handleReset}
          style={{
            border: "1px solid var(--color-border, #30363d)",
            background: "rgba(88, 166, 255, 0.14)",
            color: "var(--color-text, #e6edf3)",
            borderRadius: "8px",
            padding: "8px 14px",
            cursor: "pointer",
            fontSize: "0.84rem",
          }}
        >
          Try again
        </button>
      </div>
    );
  }
}
