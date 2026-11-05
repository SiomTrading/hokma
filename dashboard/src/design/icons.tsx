import type { SVGProps } from "react";

const BOX = "M6 4h12a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3z";

/** Stroke icons of the design, 24x24 grid. */
export const ICONS = {
  plus: "M12 5v14M5 12h14",
  close: "M6 6l12 12M18 6L6 18",
  sidebar: `${BOX}M9 4v16`,
  rightPanel: `${BOX}M15 4v16`,
  terminal: `${BOX}M7 9l3 3-3 3M13 15h4`,
  fullscreen: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
  chat: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
  panel: "M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z",
  chart: "M3 3v18h18M8 17V9M12 17V5M16 17v-6M20 17v-3",
  tasks: "M12 6v6l4 2M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z",
  learn: "M2 4h7a4 4 0 0 1 3 1.5A4 4 0 0 1 15 4h7v15h-7a3 3 0 0 0-3 2 3 3 0 0 0-3-2H2z",
  settings:
    "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1",
  general: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM4 12h2M18 12h2M12 4v2M12 18v2",
  agents:
    "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8",
  models: "M3 3v18h18M7 15l4-4 3 3 5-6",
  extensions: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM16.5 13v7M13 16.5h7",
  status: "M22 12h-4l-3 9L9 3l-3 9H2",
  news: "M12 2l3 7h7l-5.5 4.5 2 7.5-6.5-4-6.5 4 2-7.5L2 9h7z",
  chevronUp: "M6 15l6-6 6 6",
  chevronDown: "M6 9l6 6 6-6",
  chevronRight: "M9 6l6 6-6 6",
  send: "M12 19V5M5 12l7-7 7 7",
  mic: "M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3zM19 10v2a7 7 0 0 1-14 0v-2M12 19v3",
  image: "M6 3h12a3 3 0 0 1 3 3v12a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3V6a3 3 0 0 1 3-3zM7 9a2 2 0 1 0 4 0 2 2 0 0 0-4 0zM21 15l-5-5L5 21",
  file: "M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM21 21l-4.3-4.3",
} as const;

export type IconName = keyof typeof ICONS;

interface IconProps extends Omit<SVGProps<SVGSVGElement>, "name"> {
  name: IconName;
  size?: number;
  strokeWidth?: number;
}

export function Icon({ name, size = 17, strokeWidth = 1.8, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      <path d={ICONS[name]} />
    </svg>
  );
}

interface IsotypeProps extends SVGProps<SVGSVGElement> {
  size?: number;
  /** Single color (watermark) instead of white + accent. */
  mono?: boolean;
}

/** Hokma isotype, 256x256 grid. */
export function Isotype({ size = 30, mono = false, style, ...rest }: IsotypeProps) {
  const accent = mono ? "currentColor" : "var(--acc)";
  return (
    <svg width={size} height={size} viewBox="0 0 256 256" aria-hidden="true" style={{ flex: "none", ...(mono ? {} : { color: "var(--t1)" }), ...style }} {...rest}>
      <polygon fill="currentColor" points="147.35 101.34 103.94 156.44 51.38 156.47 51.31 207.75 0 207.71 .02 48.14 51.37 48.13 51.39 118.66 69.91 118.73 122.12 48.22 147.35 101.34" />
      <polygon fill={accent} points="246.2 185.32 256 207.73 204.69 207.87 165.54 125.27 129.61 48.19 182.68 48.21 210.27 106.95 246.2 185.32" />
      <polygon fill="currentColor" points="198.45 207.87 128.61 207.87 106.76 162.31 150.17 107.21 163.43 135.01 198.45 207.87" />
    </svg>
  );
}
