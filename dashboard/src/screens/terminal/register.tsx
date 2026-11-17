import { registerSlot } from "@/shell/registry";
import { TerminalBridge } from "./TerminalBridge";
import { TerminalPanel } from "./TerminalPanel";

registerSlot("terminal", TerminalPanel, { order: 0 });
registerSlot("statusBar.end", TerminalBridge, { order: 100 });
