import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { mockApi } from "../system/testkit";
import { TelegramSection, telegramStatus } from "./TelegramSection";

afterEach(() => vi.unstubAllGlobals());

const off = { linked: false, connected: false, user_name: "" };

describe("telegramStatus", () => {
  it("derives the state", () => {
    expect(telegramStatus(off)).toBe("off");
    expect(telegramStatus({ ...off, connected: true })).toBe("waiting");
    expect(telegramStatus({ linked: true, connected: true, user_name: "Ana" })).toBe("linked");
  });
});

describe.each(["es", "en"] as const)("TelegramSection (%s)", (locale) => {
  const T = (key: string) => t(key, undefined, locale);

  it("connects with the pasted token, then approves the code", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/telegram": off,
      "PUT /api/telegram/token": { connected: true },
      "POST /api/telegram/approve": { linked: true, user_name: "Ana" },
    });
    renderWithApp(<TelegramSection />);
    expect(await screen.findByTestId("telegram-status")).toHaveTextContent(T("telegram.status.off"));
    await userEvent.type(screen.getByLabelText(T("telegram.token")), " 123:abc ");
    await userEvent.click(screen.getByRole("button", { name: T("telegram.connect") }));
    expect(await screen.findByText(T("telegram.instruction"))).toBeInTheDocument();
    expect(screen.getByTestId("telegram-status")).toHaveTextContent(T("telegram.status.waiting"));
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ token: "123:abc" });

    await userEvent.type(screen.getByLabelText(T("telegram.code")), "ABCD1234");
    await userEvent.click(screen.getByRole("button", { name: T("telegram.approve") }));
    await waitFor(() => expect(screen.getByTestId("telegram-status")).toHaveTextContent(T("telegram.status.linked")));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ code: "ABCD1234" });
    expect(screen.queryByLabelText(T("telegram.code"))).not.toBeInTheDocument();
  });

  it("unlinks and goes back to the token form", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/telegram": { linked: true, connected: true, user_name: "Ana" },
      "DELETE /api/telegram": { linked: false },
    });
    renderWithApp(<TelegramSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("telegram.unlink") }));
    expect(await screen.findByLabelText(T("telegram.token"))).toBeInTheDocument();
    expect(calls.some((c) => c.method === "DELETE")).toBe(true);
  });

  it("shows the API error and keeps the form when the token is refused", async () => {
    setLocale(locale);
    mockApi({
      "GET /api/telegram": off,
      "PUT /api/telegram/token": new Response(JSON.stringify({ detail: { code: "invalid_token", message: "Bad token" } }), { status: 400 }),
    });
    renderWithApp(<TelegramSection />);
    await userEvent.type(await screen.findByLabelText(T("telegram.token")), "x");
    await userEvent.click(screen.getByRole("button", { name: T("telegram.connect") }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Bad token");
    expect(screen.getByTestId("telegram-status")).toHaveTextContent(T("telegram.status.off"));
  });
});
