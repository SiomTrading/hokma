import type { ReactNode } from "react";
import { cx } from "./cx";
import { Icon } from "./icons";
import type { IconName } from "./icons";
import { Button } from "./ui";
import { useI18n } from "@/i18n";

interface EmptyStateProps {
  icon?: IconName;
  title: string;
  description?: string;
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div className={cx("state fade", className)}>
      {icon ? <Icon name={icon} size={26} strokeWidth={1.6} style={{ color: "var(--t3)" }} /> : null}
      <h2 className="h2">{title}</h2>
      {description ? <p className="p">{description}</p> : null}
      {action}
    </div>
  );
}

interface LoadingProps {
  /** Text read by assistive tech and shown next to the spinner. Default: translated "Loading". */
  label?: string;
  /** Single line instead of a centered block. */
  inline?: boolean;
  className?: string;
}

export function Loading({ label, inline, className }: LoadingProps) {
  const { t } = useI18n();
  const text = label ?? t("ui.loading");
  return (
    <div className={cx("state", inline && "row-state", className)} role="status" aria-live="polite">
      <span className="spin" aria-hidden="true" />
      <span className="p">{text}</span>
    </div>
  );
}

interface ErrorStateProps {
  title?: string;
  /** User-safe message (ApiError.message). Never pass stack traces. */
  message?: string;
  onRetry?: () => void;
  className?: string;
}

export function ErrorState({ title, message, onRetry, className }: ErrorStateProps) {
  const { t } = useI18n();
  return (
    <div className={cx("state fade", className)} role="alert">
      <span className="pill bad">{title ?? t("ui.error.title")}</span>
      {message ? <p className="p">{message}</p> : null}
      {onRetry ? (
        <Button size="sm" onClick={onRetry}>
          {t("ui.error.retry")}
        </Button>
      ) : null}
    </div>
  );
}

export type ToastTone = "ok" | "warn" | "bad" | "info";

export interface ToastItem {
  id: number;
  tone: ToastTone;
  text: string;
}

interface ToasterProps {
  items: readonly ToastItem[];
  onDismiss: (id: number) => void;
  dismissLabel: string;
}

/** Presentational stack. Use `shell.toast(...)` to show one. */
export function Toaster({ items, onDismiss, dismissLabel }: ToasterProps) {
  return (
    <div className="toasts" aria-live="polite">
      {items.map((item) => (
        <div key={item.id} className={cx("toast", item.tone)} role={item.tone === "bad" ? "alert" : "status"}>
          <span className="dot" aria-hidden="true" />
          <span>{item.text}</span>
          <button type="button" className="tx" aria-label={dismissLabel} onClick={() => onDismiss(item.id)}>
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
