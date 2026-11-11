/** Frames of WS /ws/terminal. */
export type ServerFrame =
  | { type: "ready"; pid: number; cwd: string }
  | { type: "output"; data: string }
  | { type: "exit"; code: number | null }
  | { type: "error"; code: string; detail?: string }
  | { type: "pong" };

export type ClientFrame = { type: "input"; data: string } | { type: "resize"; cols: number; rows: number } | { type: "ping" };

export type TermStatus = "connecting" | "ready" | "exited" | "error" | "closed";

/** What the panel renders for one tab. */
export interface TermTabView {
  id: string;
  n: number;
  status: TermStatus;
  cwd: string | null;
}

export interface LaunchOptions {
  /** Typed into the shell (with Enter) as soon as it is ready. */
  command?: string;
  /** Project whose folder the shell opens in. Default: the active project, else the working folder. */
  projectId?: string | null;
}
