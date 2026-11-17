import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { mockApi } from "../system/testkit";
import { TelegramSection, telegramStatus, type TelegramState } from "./TelegramSection";

afterEach(() => vi.unstubAllGlobals());

const off: TelegramState = { linked: false, connected: false, user_name: "" };

/** A server that remembers: the screen rereads it after every click, as it does against the real one. */
function server(initial: TelegramState, routes: Record<string, (state: TelegramState) => Partial<TelegramState>>) {
  let state = initial;
  const handlers: Record<string, unknown> = { "GET /api/telegram": () => state };
  for (const [key, change] of Object.entries(routes)) {
    handlers[key] = () => {
      const patch = change(state);
      state = { ...state, ...patch };
      return patch;
    };
  }
  return mockApi(handlers);
}

describe("telegramStatus", () => {
  it("derives the state", () => {
    expect(telegramStatus(off)).toBe("off");
    expect(telegramStatus({ ...off, connected: true })).toBe("waiting");
    expect(telegramStatus({ linked: true, connected: true, user_name: "Ana" })).toBe("linked");
  });

  it("a link without a live bot is offline, never linked (bug 1126)", () => {
    expect(telegramStatus({ linked: true, connected: false, user_name: "Ana" })).toBe("offline");
  });
});

describe.each(["es", "en"] as const)("TelegramSection (%s)", (locale) => {
  const T = (key: string, vars?: Record<string, string | number>) => t(key, vars, locale);

  it("connects with the pasted token, then approves the code", async () => {
    setLocale(locale);
    const calls = server(off, {
      "PUT /api/telegram/token": () => ({ connected: true }),
      "POST /api/telegram/approve": () => ({ linked: true, user_name: "Ana" }),
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
    const calls = server({ linked: true, connected: true, user_name: "Ana" }, {
      "DELETE /api/telegram": () => ({ linked: false, connected: false, user_name: "" }),
    });
    renderWithApp(<TelegramSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("telegram.unlink") }));
    await userEvent.click(await screen.findByRole("button", { name: T("telegram.cancel") }));          // bug 1157: asks first
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: T("telegram.unlink") }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: T("telegram.unlink") }));
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

  it("group: disabled without private link, steps and code when linked, status, unlink", async () => {
    setLocale(locale);
    mockApi({ "GET /api/telegram": off });
    const first = renderWithApp(<TelegramSection />);
    expect(await screen.findByText(T("telegram.group.needsLink"))).toBeInTheDocument();
    expect(screen.queryByLabelText(T("telegram.group.code"))).not.toBeInTheDocument();
    first.unmount();

    const calls = server({ linked: true, connected: true, user_name: "Ana", group_linked: false, avisos: null }, {
      "POST /api/telegram/group/approve": () => ({ group_linked: true, avisos: "no_permission" }),
      "DELETE /api/telegram/group": () => ({ group_linked: false, avisos: null }),
    });
    renderWithApp(<TelegramSection />);
    for (const n of [1, 2, 3, 4]) expect(await screen.findByText(T(`telegram.group.step${n}`))).toBeInTheDocument();
    const approve = screen.getAllByRole("button", { name: T("telegram.group.approve") })[0];
    expect(approve).toBeDisabled();
    await userEvent.type(screen.getByLabelText(T("telegram.group.code")), " GRP123 ");
    await userEvent.click(approve);
    expect(await screen.findByTestId("telegram-group-status")).toHaveTextContent(T("telegram.group.linked"));
    expect(screen.getByTestId("telegram-group-avisos")).toHaveTextContent(T("telegram.group.avisos.no_permission"));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ code: "GRP123" });

    await userEvent.click(screen.getByRole("button", { name: T("telegram.group.unlink") }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: T("telegram.group.unlink") }));
    await waitFor(() => expect(screen.queryByTestId("telegram-group-status")).not.toBeInTheDocument());
    expect(calls.some((c) => c.method === "DELETE")).toBe(true);
    expect(screen.getByTestId("telegram-status")).toHaveTextContent(T("telegram.status.linked"));
  });

  it("group: a missing Avisos permission offers a retry that updates the state (bug 1157)", async () => {
    setLocale(locale);
    const calls = server({ linked: true, connected: true, user_name: "Ana", group_linked: true, avisos: "no_permission" }, {
      "POST /api/telegram/group/avisos": () => ({ avisos: "ok" }),
    });
    renderWithApp(<TelegramSection />);
    expect(await screen.findByText(T("telegram.avisos.hint.no_permission"))).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: T("telegram.avisos.retry") }));
    await waitFor(() => expect(screen.getByTestId("telegram-group-avisos")).toHaveTextContent(T("telegram.group.avisos.ok")));
    expect(calls.some((c) => c.path === "/api/telegram/group/avisos")).toBe(true);
    expect(screen.queryByRole("button", { name: T("telegram.avisos.retry") })).not.toBeInTheDocument();
  });

  it("group: shows Avisos ok when the topic works", async () => {
    setLocale(locale);
    mockApi({ "GET /api/telegram": { linked: true, connected: true, user_name: "Ana", group_linked: true, avisos: "ok" } });
    renderWithApp(<TelegramSection />);
    expect(await screen.findByTestId("telegram-group-avisos")).toHaveTextContent(T("telegram.group.avisos.ok"));
  });

  it("group: an Avisos that could not be checked is not reported as a missing permission (bug 1127)", async () => {
    setLocale(locale);
    mockApi({ "GET /api/telegram": { linked: true, connected: true, user_name: "Ana", group_linked: true, avisos: "unreachable" } });
    renderWithApp(<TelegramSection />);
    expect(await screen.findByTestId("telegram-group-avisos")).toHaveTextContent(T("telegram.group.avisos.unreachable"));
  });

  it("a link without a live bot says there is no connection and Reconnect brings it back (bug 1126)", async () => {
    setLocale(locale);
    const calls = server({ linked: true, connected: false, user_name: "Ana", connect_error: "ConnectError" }, {
      "POST /api/telegram/reconnect": () => ({ connected: true }),
    });
    renderWithApp(<TelegramSection />);
    expect(await screen.findByTestId("telegram-status")).toHaveTextContent(T("telegram.status.offline"));
    expect(screen.queryByText(T("telegram.status.linked"))).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: T("telegram.reconnect") }));
    await waitFor(() => expect(screen.getByTestId("telegram-status")).toHaveTextContent(T("telegram.status.linked")));
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/telegram/reconnect")).toBe(true);
    expect(screen.queryByRole("button", { name: T("telegram.reconnect") })).not.toBeInTheDocument();
  });

  it("the screen rereads the server after a click: a result is not frozen (bug 1126)", async () => {
    setLocale(locale);
    let connected = false;
    mockApi({
      "GET /api/telegram": () => ({ linked: true, connected, user_name: "Ana" }),
      "POST /api/telegram/reconnect": () => {
        connected = true;
        return { connected: false };          // the answer lags; the next read tells the truth
      },
    });
    renderWithApp(<TelegramSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("telegram.reconnect") }));
    await waitFor(() => expect(screen.getByTestId("telegram-status")).toHaveTextContent(T("telegram.status.linked")));
  });

  it("shows what waits in the outbox and the last error (bug 1127)", async () => {
    setLocale(locale);
    mockApi({
      "GET /api/telegram": {
        linked: true, connected: true, user_name: "Ana", outbox: { pending: 2, last_error: "Forbidden: bot was blocked" },
      },
    });
    renderWithApp(<TelegramSection />);
    const box = await screen.findByTestId("telegram-outbox");
    expect(box).toHaveTextContent(T("telegram.outbox.pending", { count: 2 }));
    expect(box).toHaveTextContent("Forbidden: bot was blocked");
  });

  it("shows nothing about the outbox when it is empty", async () => {
    setLocale(locale);
    mockApi({ "GET /api/telegram": { linked: true, connected: true, user_name: "Ana", outbox: { pending: 0, last_error: null } } });
    renderWithApp(<TelegramSection />);
    await screen.findByTestId("telegram-status");
    expect(screen.queryByTestId("telegram-outbox")).not.toBeInTheDocument();
  });
});
