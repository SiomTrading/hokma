export type ExtTab = "plugins" | "mcp";
export const TAB_KEY = "hokma.extensions.tab";

export function readTab(): ExtTab {
  try {
    return localStorage.getItem(TAB_KEY) === "mcp" ? "mcp" : "plugins";
  } catch {
    return "plugins";
  }
}

export function storeTab(tab: ExtTab): void {
  try {
    localStorage.setItem(TAB_KEY, tab);
  } catch {
    /* storage blocked: the tab is just not remembered */
  }
}
