import { useEffect, useRef } from "react";
import { useI18n } from "@/i18n";
import { CHAT_WIDTH_MIN, chatWidthMax, clampChatWidth } from "./terminalPrefs";

const STEP = 20;

interface Props {
  width: number;
  /** Called while dragging (`final` false) and when the drag or key press ends (`final` true). */
  onChange: (width: number, final: boolean) => void;
}

/** The bar between the chart and the chat: drag it, or use the arrow keys, to make the chat wider or narrower. */
export function ChatSplitter({ width, onChange }: Props) {
  const { t } = useI18n();
  const drag = useRef<{ x: number; width: number } | null>(null);
  const latest = useRef(width);
  latest.current = width;
  const stop = useRef<(() => void) | null>(null);

  useEffect(() => () => stop.current?.(), []);

  const start = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    drag.current = { x: event.clientX, width: latest.current };
    const move = (e: PointerEvent) => {
      if (!drag.current) return;
      // The chat is on the right: moving the bar to the left makes it wider.
      const next = clampChatWidth(drag.current.width + (drag.current.x - e.clientX));
      latest.current = next;
      onChange(next, false);
    };
    const end = () => {
      stop.current?.();
      onChange(latest.current, true);
    };
    stop.current = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      drag.current = null;
      stop.current = null;
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
  };

  const key = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const delta = event.key === "ArrowLeft" ? STEP : event.key === "ArrowRight" ? -STEP : 0;
    if (delta === 0) return;
    event.preventDefault();
    onChange(clampChatWidth(width + delta), true);
  };

  return (
    <div
      className="dterm-split"
      role="separator"
      aria-orientation="vertical"
      aria-label={t("desks.terminal.resize")}
      aria-valuemin={CHAT_WIDTH_MIN}
      aria-valuemax={chatWidthMax()}
      aria-valuenow={width}
      tabIndex={0}
      onPointerDown={start}
      onKeyDown={key}
    />
  );
}
