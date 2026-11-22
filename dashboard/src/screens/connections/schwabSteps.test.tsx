import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { installFakeSockets } from "@/test/fakeSocket";
import { BAD_KEY_TEST, FakeConnections, SCHWAB_AUTH_URL, SCHWAB_REDIRECT, SECRET } from "@/test/connectionsBackend";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { connectionErrorText } from "./api";
import { ConnectionsScreen } from "./ConnectionsScreen";
import { requestFocus } from "./focus";
import { SCHWAB_CALLBACK } from "./SchwabSteps";
import { whenText } from "./session";

let backend: FakeConnections;
const CLIENT_ID = "FAKE-CLIENT-ID-0000";

const rowOf = (id: string) => screen.getByTestId("connection-" + id);
const dialog = () => screen.getByRole("dialog");
const nowSec = () => Math.floor(Date.now() / 1000);
const session = (state: string, inSeconds: number) => ({ state, session_expires_at: nowSec() + inSeconds, accounts: [{ account_ref: "A1", mask: "••••1234" }] });

async function show() {
  const view = renderWithApp(<ConnectionsScreen />);
  await screen.findByTestId("connection-coinbase");
  return view;
}

async function chooseSchwab() {
  await userEvent.click(screen.getByRole("button", { name: t("connections.add") }));
  const list = await screen.findByRole("dialog", { name: t("connections.add.title") });
  await userEvent.click(within(list).getByRole("button", { name: t("connections.add.choose"), description: "Schwab" }));
}

async function fillStep1() {
  await userEvent.type(screen.getByLabelText(t("connections.field.clientId")), CLIENT_ID);
  await userEvent.type(screen.getByLabelText(t("connections.field.clientSecret")), SECRET);
  await userEvent.click(screen.getByRole("button", { name: t("connections.schwab.saveNext") }));
  await screen.findByRole("heading", { name: t("connections.schwab.step2") });
}

function fakeTab() {
  const tab = { opener: "x" as unknown, location: { href: "" }, close: vi.fn() };
  vi.stubGlobal("open", vi.fn(() => tab));
  return tab;
}

beforeEach(() => {
  backend = new FakeConnections();
  clearToken();
  setToken(TEST_TOKEN);
  setLocale("en");
  installFakeSockets();
  vi.stubGlobal("fetch", vi.fn(backend.handle));
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete (window as { __TAURI__?: unknown }).__TAURI__;
});

describe("Schwab in three steps", () => {
  it("goes from the keys to the sign-in to the test and leaves Schwab connected and first", async () => {
    const tab = fakeTab();
    await show();
    await chooseSchwab();

    // step 1
    expect(screen.getByRole("heading", { name: t("connections.schwab.step1") })).toBeInTheDocument();
    expect(screen.getByLabelText(t("connections.field.clientId"))).toHaveAttribute("type", "password");
    expect(screen.getByLabelText(t("connections.field.clientSecret"))).toHaveAttribute("type", "password");
    expect(screen.getByTestId("schwab-callback")).toHaveTextContent(t("connections.schwab.callback", { url: SCHWAB_CALLBACK }));
    const save = screen.getByRole("button", { name: t("connections.schwab.saveNext") });
    expect(save).toBeDisabled();
    await userEvent.type(screen.getByLabelText(t("connections.field.clientId")), CLIENT_ID);
    expect(save).toBeDisabled(); // both are needed
    await userEvent.type(screen.getByLabelText(t("connections.field.clientSecret")), SECRET);
    await userEvent.click(save);

    // step 2: the keys went out once and are gone
    await screen.findByRole("heading", { name: t("connections.schwab.step2") });
    expect(backend.sent("PUT", "/schwab/key")[0]!.body).toEqual({ client_id: CLIENT_ID, client_secret: SECRET });
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(document.body.innerHTML).not.toContain(CLIENT_ID);
    expect(screen.getByText(t("connections.schwab.step2.a"))).toBeInTheDocument();
    expect(screen.getByText(t("connections.schwab.step2.b"))).toBeInTheDocument();
    expect(screen.getByText(t("connections.schwab.step2.c"))).toBeInTheDocument();
    expect(screen.queryByLabelText(t("connections.field.clientId"))).toBeNull();
    expect(screen.queryByTestId("schwab-relogin")).toBeNull(); // not connected yet
    expect(screen.getByRole("button", { name: t("connections.guide.title") })).toHaveAttribute("aria-expanded", "false");

    await userEvent.click(screen.getByRole("button", { name: t("connections.schwab.login") }));
    await waitFor(() => expect(tab.location.href).toBe(SCHWAB_AUTH_URL));
    expect(window.open).toHaveBeenCalledWith("about:blank", "_blank");
    expect(tab.opener).toBeNull();
    expect(backend.sent("POST", "/schwab/login/start")).toHaveLength(1);
    expect(backend.sent("POST", "/schwab/login/open")).toHaveLength(0);

    const connect = screen.getByRole("button", { name: t("connections.schwab.connect") });
    expect(connect).toBeDisabled();
    await userEvent.type(screen.getByLabelText(t("connections.schwab.paste")), "placeholder");
    await userEvent.clear(screen.getByLabelText(t("connections.schwab.paste")));
    await userEvent.click(screen.getByLabelText(t("connections.schwab.paste")));
    await userEvent.paste(SCHWAB_REDIRECT);
    await userEvent.click(connect);

    // step 3
    expect(await screen.findByTestId("schwab-added")).toHaveTextContent("Schwab was added.");
    expect(backend.sent("POST", "/schwab/login/finish")[0]!.body).toEqual({ redirect_url: SCHWAB_REDIRECT });
    expect(screen.getByRole("heading", { name: t("connections.schwab.step3") })).toBeInTheDocument();
    expect(screen.getByTestId("schwab-test-state")).toHaveTextContent("Connected · Live · 240 ms");
    expect(screen.getByTestId("schwab-account")).toHaveTextContent("Account ••••1234");
    expect(document.body.innerHTML).not.toContain("FAKECODE");
    await userEvent.click(screen.getByRole("button", { name: t("connections.add.done") }));
    expect(screen.queryByRole("dialog")).toBeNull();

    // the row, and Schwab first where it serves
    await waitFor(() => expect(screen.getByTestId("connection-schwab")).toBeInTheDocument());
    const row = within(rowOf("schwab"));
    expect(row.getByTestId("conn-detail")).toHaveTextContent("Account ••••1234");
    expect(row.getByTestId("conn-state")).toHaveTextContent(t("connections.state.connected"));
    expect(row.getByTestId("conn-data")).toHaveTextContent(t("connections.data.live"));
    expect(row.getByTestId("conn-session")).toHaveTextContent("Session ends in 7 d");
  });

  it("step 1 has the guide open and foldable; the guide of Schwab is the one of 7.4", async () => {
    await show();
    await chooseSchwab();
    const fold = screen.getByRole("button", { name: t("connections.guide.title") });
    expect(fold).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("guide-what")).toHaveTextContent(t("connections.guide.schwab.what"));
    await userEvent.click(fold);
    expect(fold).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByTestId("guide-steps")).not.toBeVisible();
  });

  it("Copy puts the callback address in the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await show();
    await chooseSchwab();
    fireEvent.click(screen.getByRole("button", { name: t("connections.schwab.copy") }));
    expect(writeText).toHaveBeenCalledWith("https://127.0.0.1:8182");
    expect(SCHWAB_CALLBACK).toBe("https://127.0.0.1:8182");
  });

  it("in the desktop window it asks the engine to open the browser instead of opening a tab", async () => {
    Object.defineProperty(window, "__TAURI__", { value: {}, configurable: true });
    const open = vi.fn();
    vi.stubGlobal("open", open);
    await show();
    await chooseSchwab();
    await fillStep1();
    await userEvent.click(screen.getByRole("button", { name: t("connections.schwab.login") }));
    await waitFor(() => expect(backend.sent("POST", "/schwab/login/open")).toHaveLength(1));
    expect(backend.sent("POST", "/schwab/login/open")[0]!.body).toEqual({ url: SCHWAB_AUTH_URL });
    expect(open).not.toHaveBeenCalled();
  });

  it("when the engine cannot start the sign-in the tab is closed and the error and guide show", async () => {
    const tab = fakeTab();
    backend.startFails = { status: 409, code: "connections.no_key" };
    await show();
    await chooseSchwab();
    await fillStep1();
    await userEvent.click(screen.getByRole("button", { name: t("connections.schwab.login") }));
    expect(await screen.findByTestId("schwab-error")).toHaveTextContent(t("connections.err.no_key"));
    expect(tab.close).toHaveBeenCalled();
  });

  it("a test that fails after the sign-in says Not added and offers Back", async () => {
    backend.testAnswer = BAD_KEY_TEST;
    fakeTab();
    await show();
    await chooseSchwab();
    await fillStep1();
    await userEvent.click(screen.getByLabelText(t("connections.schwab.paste")));
    await userEvent.paste(SCHWAB_REDIRECT);
    await userEvent.click(screen.getByRole("button", { name: t("connections.schwab.connect") }));
    expect(await screen.findByTestId("schwab-test-failed")).toHaveTextContent("Not added: The provider did not accept the key. Check it and try again.");
    expect(screen.getByTestId("guide-fail")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: t("connections.add.back") }));
    expect(screen.getByRole("heading", { name: t("connections.schwab.step2") })).toBeInTheDocument();
  });
});

describe("the errors of the sign-in, each with its text and the guide", () => {
  const CODES = [
    [422, "schwab_url"],
    [409, "schwab_state"],
    [400, "schwab_code"],
    [403, "schwab_denied"],
    [409, "no_login"],
  ] as const;

  for (const locale of ["en", "es"] as const) {
    for (const [status, code] of CODES) {
      it(`${code} in ${locale}`, async () => {
        setLocale(locale);
        backend.find("schwab").key_state = "saved";
        backend.finishFails = { status, code: "connections." + code };
        requestFocus(null, { provider: "schwab", login: true });
        await show();
        await screen.findByRole("dialog");
        await userEvent.click(screen.getByLabelText(t("connections.schwab.paste")));
        await userEvent.paste(SCHWAB_REDIRECT);
        await userEvent.click(screen.getByRole("button", { name: t("connections.schwab.connect") }));
        const error = await screen.findByTestId("schwab-error");
        expect(error).toHaveTextContent(t("connections.err." + code));
        expect(t("connections.err." + code)).not.toBe("connections.err." + code);
        // back on step 2, with the guide "If the test fails" under the error
        expect(screen.getByRole("heading", { name: t("connections.schwab.step2") })).toBeInTheDocument();
        expect(within(dialog()).getByTestId("guide-fail")).toHaveTextContent(t("connections.guide.schwab.fail"));
        expect(screen.getByLabelText(t("connections.schwab.paste"))).toHaveValue(SCHWAB_REDIRECT); // can be corrected
      });
    }
  }
});

describe("schwab_expired", () => {
  // The engine answers it with 401 (5.12). The text is checked on the error itself: api/client.ts treats every 401 whose code is
  // not `options.*` as the end of the Hokma session, so that code has to be allowed there too (see the report of T10).
  for (const locale of ["en", "es"] as const) {
    it(`has its text in ${locale}`, () => {
      setLocale(locale);
      const text = connectionErrorText(new ApiError(401, "connections.schwab_expired", "server text"));
      expect(text).toBe(t("connections.err.schwab_expired"));
      expect(text).not.toBe("connections.err.schwab_expired");
      expect(text).not.toBe("server text");
    });
  }
});

describe("the notice and the sign-in step", () => {
  it("login=1 opens step 2 without asking for the Client ID when the keys are saved, and only once", async () => {
    backend.find("schwab").key_state = "saved";
    requestFocus(null, { provider: "schwab", login: true });
    const first = await show();
    const open = await screen.findByRole("dialog", { name: t("connections.add.formTitle", { name: "Schwab" }) });
    expect(within(open).getByRole("heading", { name: t("connections.schwab.step2") })).toBeInTheDocument();
    expect(open.querySelector("[data-field='client_id']")).toBeNull();
    expect(within(open).getByLabelText(t("connections.schwab.paste"))).toBeInTheDocument();
    first.unmount();
    // the screen opens again later: the same request does not open the dialog again
    await show();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("login=1 without saved keys starts at step 1", async () => {
    requestFocus(null, { provider: "schwab", login: true });
    await show();
    await screen.findByRole("dialog");
    expect(screen.getByRole("heading", { name: t("connections.schwab.step1") })).toBeInTheDocument();
  });

  it("with Schwab connected, step 2 warns that an unfinished sign-in can close the session", async () => {
    Object.assign(backend.find("schwab"), { added: true, key_state: "saved", state: "connected", session: session("signed_in", 3 * 86400) });
    requestFocus(null, { provider: "schwab", login: true });
    await show();
    expect(await screen.findByTestId("schwab-relogin")).toHaveTextContent(t("connections.schwab.reloginNote"));
  });

  it("the strip shows 24 hours before the end with the button, and the button opens step 2", async () => {
    const ends = nowSec() + 20 * 3600;
    Object.assign(backend.find("schwab"), { added: true, key_state: "saved", state: "connected", session: { ...session("expiring", 20 * 3600), session_expires_at: ends } });
    await show();
    const band = await screen.findByTestId("schwab-band");
    expect(band).toHaveTextContent(t("connections.schwab.expiresSoon", { when: whenText(new Date(ends * 1000), "en") }));
    await userEvent.click(within(band).getByRole("button", { name: t("connections.schwab.login") }));
    const open = await screen.findByRole("dialog");
    expect(within(open).getByRole("heading", { name: t("connections.schwab.step2") })).toBeInTheDocument();
  });

  it("no strip while the session is fine; an ended session shows its text on the strip and on the row", async () => {
    Object.assign(backend.find("schwab"), { added: true, key_state: "saved", state: "connected", session: session("signed_in", 5 * 86400) });
    const view = await show();
    expect(screen.queryByTestId("schwab-band")).toBeNull();
    view.unmount();
    Object.assign(backend.find("schwab"), { state: "error", error_code: "connections.schwab_expired", session: session("expired", -10) });
    await show();
    expect(await screen.findByTestId("schwab-band")).toHaveTextContent(t("connections.schwab.expired"));
    expect(within(rowOf("schwab")).getByTestId("conn-error")).toHaveTextContent("Your Schwab session ended. Sign in again.");
  });

  it("the menu of the Schwab row adds Sign in with Schwab, View account and Check Schwab data", async () => {
    Object.assign(backend.find("schwab"), { added: true, key_state: "saved", state: "connected", session: session("signed_in", 5 * 86400) });
    await show();
    await userEvent.click(screen.getByRole("button", { name: t("connections.row.menu", { name: "Schwab" }) }));
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      t("connections.test"), t("connections.row.pause"), t("connections.row.changeKey"), t("connections.schwab.login"), t("connections.schwab.viewAccount"), t("connections.schwab.diagnostic"), t("connections.row.guide"), t("connections.row.remove"),
    ]);
  });

  it("Remove Schwab asks first and then the row leaves", async () => {
    Object.assign(backend.find("schwab"), { added: true, key_state: "saved", state: "connected", session: session("signed_in", 5 * 86400) });
    await show();
    await userEvent.click(screen.getByRole("button", { name: t("connections.row.menu", { name: "Schwab" }) }));
    await userEvent.click(screen.getByRole("menuitem", { name: t("connections.row.remove") }));
    const ask = await screen.findByRole("alertdialog");
    expect(ask).toHaveTextContent("Remove Schwab: Hokma deletes its key from this computer and stops using it.");
    await userEvent.click(within(ask).getByRole("button", { name: t("connections.row.remove") }));
    await waitFor(() => expect(screen.queryByTestId("connection-schwab")).toBeNull());
    expect(backend.sent("POST", "/schwab/remove")).toHaveLength(1);
  });
});

describe("the home notice", () => {
  it("shows on the home screen as the same strip and the button goes to Connections with the sign-in open", async () => {
    Object.assign(backend.find("schwab"), { added: true, key_state: "saved", state: "connected", session: session("expiring", 3600) });
    const { HomeSchwabBand } = await import("./SchwabBand");
    renderWithApp(<HomeSchwabBand />);
    const band = await screen.findByTestId("schwab-band");
    await act(async () => {
      fireEvent.click(within(band).getByRole("button", { name: t("connections.schwab.login") }));
    });
    expect(window.location.hash).toBe("#/connections?focus=schwab&login=1");
    window.history.replaceState(null, "", "/");
  });

  it("shows nothing when the session is fine or the list cannot be read", async () => {
    const { HomeSchwabBand } = await import("./SchwabBand");
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 500 })));
    const view = renderWithApp(<HomeSchwabBand />);
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(screen.queryByTestId("schwab-band")).toBeNull();
    view.unmount();
  });
});
