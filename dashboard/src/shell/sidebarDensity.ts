import { useLayoutEffect, useState } from "react";
import type { RefObject } from "react";
import { SIDEBAR_SCREENS } from "./screens";

/**
 * How tight the left menu is drawn, from the height the menu really has (the window minus the top bar,
 * the status bar and the Terminal when it is open). The CSS in shell.css draws each level:
 * - normal: the approved design.
 * - compact: shorter rows, smaller gaps and separators.
 * - tight: compact and the Recents block folded away, so Learn, Settings and Shut down always fit.
 * Below the tight height the menu scrolls by itself (overflow-y: auto) instead of cutting the bottom.
 */
export type SidebarDensity = "normal" | "compact" | "tight";

/** Pixel sizes of each level, mirrored from shell.css. */
export const DENSITY_METRICS: Record<SidebarDensity, { row: number; gap: number; sep: number; brand: number; padding: number; recents: number }> = {
  normal: { row: 36, gap: 4, sep: 13, brand: 40, padding: 20, recents: 120 },
  compact: { row: 30, gap: 2, sep: 7, brand: 36, padding: 16, recents: 96 },
  tight: { row: 27, gap: 2, sep: 7, brand: 34, padding: 12, recents: 0 },
};

/** Rows of the menu: New conversation, the screens, and the footer (Learn, Settings, Shut down). */
const ROWS = 1 + SIDEBAR_SCREENS.length + 3;

/** The height the whole menu needs at a given level, with its bottom buttons visible. */
export function requiredSidebarHeight(density: SidebarDensity): number {
  const m = DENSITY_METRICS[density];
  const withRecents = m.recents > 0;
  const separators = withRecents ? 3 : 2;
  const children = 1 + ROWS + separators + (withRecents ? 1 : 0);
  return m.padding + m.brand + ROWS * m.row + separators * m.sep + m.recents + (children - 1) * m.gap;
}

/** The roomiest level that fits in the menu height. 0 (not measured yet) keeps the design as is. */
export function sidebarDensity(height: number): SidebarDensity {
  if (height <= 0 || height >= requiredSidebarHeight("normal")) return "normal";
  if (height >= requiredSidebarHeight("compact")) return "compact";
  return "tight";
}

/** Measures the menu and follows every change of its height: window resized, Terminal opened or dragged. */
export function useSidebarDensity(ref: RefObject<HTMLElement | null>): SidebarDensity {
  const [density, setDensity] = useState<SidebarDensity>("normal");
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setDensity(sidebarDensity(el.clientHeight));
    measure();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return density;
}
