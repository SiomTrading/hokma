import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "@/App";
import { clearToken, getToken, openSocket } from "@/api";
import { setLocale, t } from "@/i18n";
import { RestartOverlay } from "@/screens/system/RestartOverlay";
import { renderShell } from "@/test/utils";
import { getRestartState, resetRestartForTests, watchEngine } from "./restart";
import { shutdownFromEntry } from "./shutdown";
import { setToken } from "./token";

const TOKEN = "abcDEF1234567890_-xyz";

function answer(status: number, body: unknown) {
  const fetchMock = vi.fn(async (url: string) =>
    url === "/api/system/shutdown-nonce"
      ? new Response(JSON.stringify({ nonce: "n".repeat(32) }), { status: 200, headers: { "Content-Type": "application/json" } })
      : new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  resetRestartForTests();
  setLocale("es");
});
afterEach(() => {
  resetRestartForTests();
  vi.unstubAllGlobals();
});

describe("the shutdown screen", () => {
  it("says Hokma is shut down, in both languages, with no buttons", async () => {
    answer(202, { stopping: true });
    render(<RestartOverlay />);
    await act(async () => shutdownFromEntry());
    expect(screen.getByRole("heading", { name: t("shutdown.offTitle") })).toBeInTheDocument();
    expect(screen.getByText(t("shutdown.offBody"))).toBeInTheDocument();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    act(() => resetRestartForTests());
    setLocale("en");
    answer(202, { stopping: true });
    await act(async () => shutdownFromEntry());
    expect(screen.getByRole("heading", { name: t("shutdown.offTitle") })).toBeInTheDocument();
  });

  it("does not turn into 'could not start' and does not watch /health", async () => {
    vi.useFakeTimers();
    try {
      const fetchMock = answer(202, { stopping: true });
      await act(async () => shutdownFromEntry());
      fetchMock.mockClear();
      act(() => watchEngine({ mode: "restart", reload: vi.fn() }));
      act(() => watchEngine({ mode: "crash", reload: vi.fn() }));
      await act(async () => void (await vi.advanceTimersByTimeAsync(70_000)));
      expect(getRestartState().phase).toBe("off");
      expect(fetchMock).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("sockets", () => {
  class FakeSocket {
    static instances: FakeSocket[] = [];
    onclose: ((e: { code: number; reason: string }) => void) | null = null;
    onopen = null;
    onmessage = null;
    onerror = null;
    constructor(public url: string) {
      FakeSocket.instances.push(this);
    }
    send() {}
    close() {}
  }

  beforeEach(() => {
    FakeSocket.instances = [];
    clearToken();
    setToken(TOKEN);
    vi.stubGlobal("WebSocket", FakeSocket);
  });

  it("a close with the reason 'shutting_down' shows the shutdown screen, keeps no timer and does not reconnect", () => {
    vi.useFakeTimers();
    try {
      answer(200, { status: "ok", version: "0.0.6" });
      openSocket("/ws/chat");
      FakeSocket.instances[0]!.onclose?.({ code: 1001, reason: "shutting_down" });
      expect(getRestartState().phase).toBe("off");
      vi.advanceTimersByTime(30_000);
      expect(FakeSocket.instances).toHaveLength(1);
      expect(fetch).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("another socket that drops afterwards (the engine is gone) does not start a crash watch", () => {
    answer(200, { status: "ok", version: "0.0.6" });
    openSocket("/ws/chat");
    openSocket("/ws/terminal");
    FakeSocket.instances[0]!.onclose?.({ code: 1001, reason: "shutting_down" });
    FakeSocket.instances[1]!.onclose?.({ code: 1006, reason: "" });
    expect(getRestartState().phase).toBe("off");
  });
});

describe("the call from the entry screen", () => {
  it("sends no token and goes to the shutdown screen on 202", async () => {
    const fetchMock = answer(202, { stopping: true });
    await shutdownFromEntry();
    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>;
    expect(calls[0]![0]).toBe("/api/system/shutdown-nonce");           // the nonce first
    const [url, init] = calls[1]!;
    expect(url).toBe("/api/system/shutdown");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["X-Hokma-Shutdown-Nonce"]).toBe("n".repeat(32));
    expect(JSON.stringify(init.headers)).not.toMatch(/x-hokma-token/i);
    expect(getRestartState().phase).toBe("off");
  });

  it("409 in_use gives the catalog text and keeps the page as it was", async () => {
    answer(409, { detail: { code: "shutdown.in_use", message: "texto del servidor" } });
    await expect(shutdownFromEntry()).rejects.toMatchObject({ status: 409, code: "shutdown.in_use", message: t("shutdown.inUse") });
    expect(getRestartState().phase).toBe("idle");
  });
});

describe("the buttons", () => {
  it("the panel has Shut down Hokma as the last item of the menu, under Settings", () => {
    renderShell();
    const menu = screen.getByRole("complementary", { name: t("nav.menu") });
    const buttons = within(menu).getAllByRole("button");
    const names = buttons.map((b) => b.getAttribute("aria-label") ?? b.textContent);
    expect(names.at(-1)).toBe(t("nav.shutdown"));
    expect(names.at(-2)).toBe(t("nav.settings"));
    expect(buttons.at(-1)).toHaveAttribute("title", t("nav.shutdown"));
  });

  it("Cancel and Escape close the question without calling anything", () => {
    const fetchMock = answer(202, {});
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: t("nav.shutdown") }));
    const dialog = screen.getByRole("alertdialog", { name: t("shutdown.confirmTitle") });
    expect(within(dialog).getByText(t("shutdown.confirmBody"))).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: t("shutdown.cancel") }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: t("nav.shutdown") }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(fetchMock.mock.calls.filter((c) => (c as unknown as [string])[0] === "/api/system/shutdown")).toHaveLength(0);
  });

  it("confirming calls the endpoint with the token and shows the shutdown screen", async () => {
    const fetchMock = answer(202, { stopping: true });
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: t("nav.shutdown") }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: t("shutdown.confirm") }));
    await waitFor(() => expect(getRestartState().phase).toBe("off"));
    const call = fetchMock.mock.calls.find((c) => (c as unknown as [string])[0] === "/api/system/shutdown") as unknown as [string, RequestInit];
    expect(call[1].method).toBe("POST");
    expect((call[1].headers as Record<string, string>)["X-Hokma-Token"]).toBeTruthy();
  });

  it("a failed call leaves the panel as it was and says why in a toast", async () => {
    answer(429, { detail: { code: "shutdown.too_soon", message: "Wait a few seconds." } });
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: t("nav.shutdown") }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: t("shutdown.confirm") }));
    expect(await screen.findByText("Wait a few seconds.")).toBeInTheDocument();
    expect(getRestartState().phase).toBe("idle");
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("the entry screen has the button, asks first and shows the in-use text under the question", async () => {
    clearToken();
    answer(409, { detail: { code: "shutdown.in_use", message: "x" } });
    render(<App />);
    fireEvent.click(screen.getByTestId("no-token-shutdown"));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: t("shutdown.confirm") }));
    expect(await screen.findByTestId("shutdown-problem")).toHaveTextContent(t("shutdown.inUse"));
    expect(getToken()).toBeNull();
    expect(getRestartState().phase).toBe("idle");
  });
});
