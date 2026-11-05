import { useEffect, useRef } from "react";
import type { ButtonHTMLAttributes, CSSProperties, HTMLAttributes, KeyboardEvent, ReactNode, RefObject } from "react";
import { cx } from "./cx";
import { Dot } from "./ui";
import { useDismiss } from "./dismiss";

/* ---------- Menu ---------- */

interface MenuProps {
  open: boolean;
  onClose: () => void;
  label: string;
  /** `below` opens under the top bar (tab menu); default opens above the composer. */
  placement?: "above" | "below";
  /** Clicks on this element do not count as "outside" (the button that toggles the menu). */
  triggerRef?: RefObject<HTMLElement | null>;
  style?: CSSProperties;
  className?: string;
  children: ReactNode;
}

export function Menu({ open, onClose, label, placement = "above", triggerRef, style, className, children }: MenuProps) {
  const ref = useRef<HTMLDivElement>(null);
  useDismiss(open, onClose);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (ref.current?.contains(target) || triggerRef?.current?.contains(target)) return;
      onClose();
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, [open, onClose, triggerRef]);

  if (!open) return null;

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const items = Array.from(ref.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
    if (items.length === 0) return;
    event.preventDefault();
    const index = items.indexOf(document.activeElement as HTMLElement);
    const next = event.key === "ArrowDown" ? (index + 1) % items.length : (index - 1 + items.length) % items.length;
    items[next]?.focus();
  };

  return (
    <div ref={ref} className={cx("menu", placement === "below" && "below", className)} role="menu" aria-label={label} style={style} onKeyDown={onKeyDown}>
      {children}
    </div>
  );
}

interface MenuItemProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  selected?: boolean;
  /** CSS color of the leading dot. Omit for no dot. */
  dotColor?: string;
  /** Right-aligned secondary text. */
  hint?: string;
  children: ReactNode;
}

export function MenuItem({ selected, dotColor, hint, className, children, type = "button", ...rest }: MenuItemProps) {
  return (
    <button type={type} role="menuitem" className={cx("mi", selected && "on", className)} {...rest}>
      {dotColor ? <Dot color={dotColor} /> : null}
      {children}
      {hint ? <span className="h">{hint}</span> : null}
    </button>
  );
}

/* ---------- Modal ---------- */

export function Scrim({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cx("scrim", className)} {...rest} />;
}

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

interface ModalProps {
  /** Accessible name of the dialog. */
  label: string;
  onClose: () => void;
  /** Close when the dark backdrop is clicked. Default true. */
  closeOnScrim?: boolean;
  style?: CSSProperties;
  className?: string;
  children: ReactNode;
}

/** Mount it only while it should be visible. It must live inside the `.hk` root. */
export function Modal({ label, onClose, closeOnScrim = true, style, className, children }: ModalProps) {
  const dialog = useRef<HTMLDivElement>(null);
  useDismiss(true, onClose);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    return () => previous?.focus?.();
  }, []);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab") return;
    const nodes = Array.from(dialog.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    if (nodes.length === 0) {
      event.preventDefault();
      return;
    }
    const first = nodes[0]!;
    const last = nodes[nodes.length - 1]!;
    if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <Scrim onMouseDown={(event) => closeOnScrim && event.target === event.currentTarget && onClose()}>
      <div ref={dialog} className={cx("modal", className)} role="dialog" aria-modal="true" aria-label={label} tabIndex={-1} style={style} onKeyDown={onKeyDown}>
        {children}
      </div>
    </Scrim>
  );
}
