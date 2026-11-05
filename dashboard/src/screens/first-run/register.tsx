import { useCallback, useEffect, useState } from "react";
import { registerSettingsSection, registerSlot } from "@/shell/registry";
import { useShellEvent } from "@/shell/useShell";
import { setupApi } from "./api";
import { FirstRun } from "./FirstRun";
import { GeneralSettings } from "./GeneralSettings";

/** Slot `brand.subtitle`: the fund's name under HOKMA, read from the profile. */
function FundName() {
  const [name, setName] = useState("");
  const load = useCallback(() => {
    setupApi.state().then(
      (s) => setName(s.profile?.fund_name ?? ""),
      () => setName(""),
    );
  }, []);
  useEffect(load, [load]);
  useShellEvent("setup:changed", load);
  return name ? <>{name}</> : null;
}

registerSlot("overlay.firstRun", FirstRun);
registerSlot("brand.subtitle", FundName);
registerSettingsSection({ id: "general", component: GeneralSettings });
