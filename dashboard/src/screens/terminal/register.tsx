import { registerSlot } from "@/shell/registry";
import { TerminalPanel } from "./TerminalPanel";

registerSlot("terminal", TerminalPanel, { order: 0 });
