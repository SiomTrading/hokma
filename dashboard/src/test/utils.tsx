import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { setToken } from "@/api";
import { Shell } from "@/shell/Shell";
import { ShellProvider } from "@/shell/store";

export const TEST_TOKEN = "test-session-token-0123456789";

/** Renders the full shell with a session token set. */
export function renderShell() {
  setToken(TEST_TOKEN);
  return render(
    <ShellProvider>
      <Shell />
    </ShellProvider>,
  );
}

/** Renders any element inside the theme root, with the shell state available. */
export function renderWithApp(ui: ReactElement, options: { light?: boolean } = {}) {
  return render(
    <ShellProvider>
      <div className={options.light ? "hk light" : "hk"}>{ui}</div>
    </ShellProvider>,
  );
}
