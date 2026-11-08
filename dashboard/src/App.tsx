import { useEffect } from "react";
import { justUpdated, useToken } from "@/api";
import { HashRoute } from "@/screens/connections/HashRoute";
import { RestartOverlay } from "@/screens/system/RestartOverlay";
import { UpdateWatcher } from "@/screens/system/UpdateWatcher";
import { NoToken, Shell } from "@/shell/Shell";
import { ShellProvider, useShell } from "@/shell/store";

/** After the page reloaded because Hokma updated itself, open Settings > News where the result is shown. */
function AfterUpdate() {
  const { openSettings } = useShell();
  useEffect(() => {
    if (justUpdated) openSettings("news");
  }, [openSettings]);
  return null;
}

export function App() {
  const token = useToken();
  return (
    <>
      {token ? (
        <ShellProvider>
          <Shell />
          <UpdateWatcher />
          <AfterUpdate />
          <HashRoute />
        </ShellProvider>
      ) : (
        <NoToken />
      )}
      <RestartOverlay />
    </>
  );
}
