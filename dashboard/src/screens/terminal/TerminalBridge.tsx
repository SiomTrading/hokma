import { useShellEvent } from "@/shell/useShell";
import { relaunchAutoTerminals } from "./store";

/**
 * Always mounted (status bar slot), unlike the dock: the wizard changing the working folder must move the automatic
 * terminal even when the dock is hidden (1165).
 */
export function TerminalBridge() {
  useShellEvent("setup:changed", () => relaunchAutoTerminals());
  return null;
}
