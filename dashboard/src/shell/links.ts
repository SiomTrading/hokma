/** A plain left click: the page handles it. The wheel, Ctrl, Cmd, Shift and Alt are left to the browser (new tab or window). */
export function isPlainClick(event: { button: number; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean; altKey: boolean }): boolean {
  return event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey;
}
