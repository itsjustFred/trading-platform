import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";
import { AlertCircle, CheckCircle2, Info, TriangleAlert } from "lucide-react";

type ToastTone = "info" | "success" | "warning" | "error";

interface ToastItem {
  id: number;
  message: string;
  tone: ToastTone;
}

interface ToastContextValue {
  showToast: (message: string, tone?: ToastTone) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

const toneStyles: Record<
  ToastTone,
  { border: string; background: string; color: string; icon: typeof Info }
> = {
  info: {
    border: "1px solid rgba(96, 165, 250, 0.28)",
    background:
      "linear-gradient(135deg, rgba(30, 41, 59, 0.96), rgba(15, 23, 42, 0.98))",
    color: "#bfdbfe",
    icon: Info,
  },
  success: {
    border: "1px solid rgba(74, 222, 128, 0.28)",
    background:
      "linear-gradient(135deg, rgba(20, 83, 45, 0.92), rgba(15, 23, 42, 0.98))",
    color: "#bbf7d0",
    icon: CheckCircle2,
  },
  warning: {
    border: "1px solid rgba(250, 204, 21, 0.28)",
    background:
      "linear-gradient(135deg, rgba(120, 53, 15, 0.94), rgba(15, 23, 42, 0.98))",
    color: "#fde68a",
    icon: TriangleAlert,
  },
  error: {
    border: "1px solid rgba(251, 113, 133, 0.28)",
    background:
      "linear-gradient(135deg, rgba(127, 29, 29, 0.94), rgba(15, 23, 42, 0.98))",
    color: "#fecdd3",
    icon: AlertCircle,
  },
};

export function ToastProvider({ children }: Readonly<{ children: ReactNode }>) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const showToast = useCallback((message: string, tone: ToastTone = "info") => {
    const id = Date.now() + Math.floor(Math.random() * 1000);
    setToasts((current) => [...current, { id, message, tone }]);
    window.setTimeout(() => {
      setToasts((current) => current.filter((toast) => toast.id !== id));
    }, 3600);
  }, []);

  const value = useMemo(() => ({ showToast }), [showToast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        style={{
          position: "fixed",
          top: "84px",
          right: "24px",
          zIndex: 90,
          display: "grid",
          gap: "10px",
          pointerEvents: "none",
          width: "min(340px, calc(100vw - 32px))",
        }}
      >
        {toasts.map((toast) => {
          const tone = toneStyles[toast.tone];
          const Icon = tone.icon;
          return (
            <div
              key={toast.id}
              style={{
                display: "flex",
                alignItems: "flex-start",
                gap: "10px",
                padding: "12px 14px",
                borderRadius: "16px",
                border: tone.border,
                background: tone.background,
                color: tone.color,
                boxShadow: "0 20px 50px rgba(0, 0, 0, 0.3)",
                pointerEvents: "auto",
              }}
            >
              <Icon size={18} style={{ flexShrink: 0, marginTop: "1px" }} />
              <div style={{ lineHeight: 1.5, fontSize: "0.88rem" }}>
                {toast.message}
              </div>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const context = useContext(ToastContext);
  if (context === null) {
    throw new Error("useToast must be used inside ToastProvider");
  }
  return context;
}
