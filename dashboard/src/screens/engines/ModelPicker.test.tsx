import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import type { Connection } from "./api";
import { ModelPicker } from "./ModelPicker";

// Model names here are test data (bug 1173: the app keeps none of its own).
const CONN: Connection = {
  id: 7, engine: "api", label: "Google", auth_kind: "api_key", provider: "google", host: "x", status: "connected",
  status_detail: null, is_default: true,
  tier_models: { fast: "m-lite", balanced: "m-new", deep: "m-new", auto: "1" },
};

let sent: { url: string; body: unknown }[] = [];

function reply(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

beforeEach(() => {
  clearToken();
  setToken(TEST_TOKEN);
  sent = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/engines/7/models")) return reply({ models: ["m-new", "m-lite", "m-old"] });
      if (url.endsWith("/api/engines/7/tiers")) {
        sent.push({ url, body: JSON.parse(String(init?.body ?? "{}")) });
        return reply(CONN);
      }
      return reply({}, 404);
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

describe.each(["es", "en"] as const)("model picker in %s (bug 1173)", (locale) => {
  it("lists the provider's models, shows what Automatic uses now, and saves one level as the student's", async () => {
    setLocale(locale);
    const saved = vi.fn();
    renderWithApp(<ModelPicker conn={CONN} onSaved={saved} />);
    await userEvent.click(screen.getByRole("button", { name: t("eng.models.open") }));
    const fast = await screen.findByLabelText(t("eng.tier.fast"));
    expect(screen.getAllByRole("option", { name: "m-old" })).toHaveLength(3);
    expect(screen.getByRole("option", { name: t("eng.models.autoNow", { model: "m-lite" }) })).toBeInTheDocument();
    await userEvent.selectOptions(fast, "m-old");
    await userEvent.click(screen.getByRole("button", { name: t("eng.models.save") }));
    expect(sent[0]!.body).toEqual({ tiers: { fast: "m-old", balanced: "auto", deep: "auto" } });
    expect(saved).toHaveBeenCalledOnce();
  });
});
