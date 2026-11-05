import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { t } from "@/i18n";
import { getSlot } from "./registry";
import type { SlotName } from "./registry";

interface BoundaryProps {
  children: ReactNode;
  /** Shown instead of the broken block. Default: a short translated notice. */
  fallback?: ReactNode;
}

/** Keeps one broken screen or slot from taking the whole shell down. The error is never shown to the user. */
export class Boundary extends Component<BoundaryProps, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("ui block failed", error.message, info.componentStack);
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return this.props.fallback ?? <p className="p" role="alert">{t("ui.slotError")}</p>;
  }
}

export function hasSlot(name: SlotName): boolean {
  return getSlot(name).length > 0;
}

export function Slot({ name, ...props }: { name: SlotName } & Record<string, unknown>) {
  const entries = getSlot(name);
  if (entries.length === 0) return null;
  return (
    <>
      {entries.map((entry, index) => {
        const Item = entry.component;
        return (
          <Boundary key={index} fallback={null}>
            <Item {...props} />
          </Boundary>
        );
      })}
    </>
  );
}
