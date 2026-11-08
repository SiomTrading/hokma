import { useCallback, useEffect, useRef } from "react";
import type { CSSProperties, ReactNode, RefObject } from "react";
import { cx, useDismiss } from "@/design";

export interface ChartPopoverProps {
  /** Accessible name of the dialog. */
  label: string;
  onClose: () => void;
  /** Where the focus goes back when the popover closes (the chart body). */
  returnFocusRef?: RefObject<HTMLElement | null>;
  /** Clicks on this element do not count as "outside" (the button that opened it). */
  triggerRef?: RefObject<HTMLElement | null>;
  className?: string;
  /** Position it next to its anchor: the popover is `position: absolute`. */
  style?: CSSProperties;
  children: ReactNode;
}

/**
 * A small non-modal popover of the chart. It fades in over 120 ms and never slides (it opens many times a day). Esc
 * and a click outside close it; the focus then goes back to `returnFocusRef`. Mount it only while it is open.
 */
export function ChartPopover({ label, onClose, returnFocusRef, triggerRef, className, style, children }: ChartPopoverProps) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const returnRef = useRef(returnFocusRef);
  returnRef.current = returnFocusRef;

  const close = useCallback(() => {
    closeRef.current();
    returnRef.current?.current?.focus();
  }, []);
  useDismiss(true, close);

  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (ref.current?.contains(target) || triggerRef?.current?.contains(target)) return;
      closeRef.current(); // a click elsewhere puts the focus where it landed
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, [triggerRef]);

  return (
    <div ref={ref} className={cx("chart-pop", className)} role="dialog" aria-modal="false" aria-label={label} style={style}>
      {children}
    </div>
  );
}

/** Closes a popover after a choice and gives the focus back to the chart. */
export function returnFocus(ref: RefObject<HTMLElement | null> | undefined): void {
  ref?.current?.focus();
}
