import { useEffect, useRef } from "react";

/**
 * Esc handling for layered UI. Menus and modals push a handler while open;
 * Esc runs only the topmost one, so a menu inside a modal closes first.
 */
const stack: Array<() => void> = [];
let attached = false;

function onKeyDown(event: KeyboardEvent): void {
  if (event.key !== "Escape" || stack.length === 0) return;
  event.preventDefault();
  event.stopPropagation();
  stack[stack.length - 1]?.();
}

function attach(): void {
  if (attached) return;
  document.addEventListener("keydown", onKeyDown, true);
  attached = true;
}

function detachIfIdle(): void {
  if (stack.length > 0 || !attached) return;
  document.removeEventListener("keydown", onKeyDown, true);
  attached = false;
}

export function useDismiss(active: boolean, onDismiss: () => void): void {
  const handler = useRef(onDismiss);
  handler.current = onDismiss;
  useEffect(() => {
    if (!active) return;
    const entry = () => handler.current();
    stack.push(entry);
    attach();
    return () => {
      const index = stack.lastIndexOf(entry);
      if (index >= 0) stack.splice(index, 1);
      detachIfIdle();
    };
  }, [active]);
}

export function hasOpenLayer(): boolean {
  return stack.length > 0;
}
