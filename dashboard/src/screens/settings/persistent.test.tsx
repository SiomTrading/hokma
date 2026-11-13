import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { mockApi } from "../system/testkit";
import { PersistentSection, wakeStatus, type PowerState } from "./PersistentSection";

afterEach(() => vi.unstubAllGlobals());

const base: PowerState = { keep_awake: false, wake_enabled: false, active: false, supported: true };

describe("wakeStatus", () => {
  it("derives the wake state", () => {
    expect(wakeStatus(base)).toBe("off");
    expect(wakeStatus({ ...base, wake_enabled: true })).toBe("ok");
    expect(wakeStatus({ ...base, wake_enabled: true, wake: { status: "needs_permission" } })).toBe("needs_permission");
    expect(wakeStatus({ ...base, supported: false })).toBe("unsupported");
    expect(wakeStatus({ ...base, wake: { status: "unsupported" } })).toBe("unsupported");
  });
});

describe.each(["es", "en"] as const)("PersistentSection (%s)", (locale) => {
  const T = (key: string) => t(key, undefined, locale);

  it("renders the switch, the wake status and the powered-off notice", async () => {
    setLocale(locale);
    mockApi({ "GET /api/power": base });
    renderWithApp(<PersistentSection />);
    const toggle = await screen.findByRole("switch", { name: T("persist.keepAwake") });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(screen.getByTestId("wake-status")).toHaveTextContent(T("persist.wake.off"));
    expect(screen.getByTestId("wake-message")).toHaveTextContent(T("persist.wake.off.message"));
    expect(screen.getByText(T("persist.powerOff"))).toBeInTheDocument();
  });

  it("PUTs the preference and shows the state the API returns", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/power": base,
      "PUT /api/power": { ...base, keep_awake: true, active: true },
    });
    renderWithApp(<PersistentSection />);
    await userEvent.click(await screen.findByRole("switch", { name: T("persist.keepAwake") }));
    await waitFor(() => expect(screen.getByRole("switch", { name: T("persist.keepAwake") })).toHaveAttribute("aria-checked", "true"));
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ keep_awake: true });
  });

  it("shows the permission message when the wake-up needs it", async () => {
    setLocale(locale);
    mockApi({ "GET /api/power": { ...base, wake_enabled: true, wake: { status: "needs_permission" } } });
    renderWithApp(<PersistentSection />);
    expect(await screen.findByTestId("wake-message")).toHaveTextContent(T("persist.wake.needs_permission.message"));
  });

  it("disables both switches on an unsupported computer", async () => {
    setLocale(locale);
    mockApi({ "GET /api/power": { ...base, supported: false } });
    renderWithApp(<PersistentSection />);
    expect(await screen.findByRole("switch", { name: T("persist.keepAwake") })).toBeDisabled();
    expect(screen.getByRole("switch", { name: T("persist.wake") })).toBeDisabled();
  });

  it("shows the error and keeps the old state when the save fails", async () => {
    setLocale(locale);
    mockApi({
      "GET /api/power": base,
      "PUT /api/power": new Response(JSON.stringify({ detail: { code: "no_profile", message: "No profile" } }), { status: 409 }),
    });
    renderWithApp(<PersistentSection />);
    await userEvent.click(await screen.findByRole("switch", { name: T("persist.keepAwake") }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: T("persist.keepAwake") })).toHaveAttribute("aria-checked", "false");
  });
});
