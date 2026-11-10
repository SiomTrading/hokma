import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { RemoteServers } from "./RemoteServers";
import { failure, mockApi } from "./testkit";

afterEach(() => vi.unstubAllGlobals());

const LAUZ = { name: "lauz", url: "https://mcp.lauz.io/mcp", message: "" };
const AUTH = "https://api.lauz.io/oauth/auth?client_id=c&state=s";

function fakeTab() {
  const tab = { opener: {} as unknown, location: { href: "about:blank" }, close: vi.fn() };
  vi.stubGlobal("open", vi.fn(() => tab));
  return tab;
}

describe.each(["es", "en"] as const)("Remote MCP servers (%s)", (locale) => {
  it("adds a server, Connect opens the sign-in in a browser tab and the list turns Connected by itself", async () => {
    setLocale(locale);
    let state = "needs_auth";
    let pending = false;
    let known = false;
    const tab = fakeTab();
    const calls = mockApi({
      "GET /api/extensions/servers": () => ({ servers: known ? [{ ...LAUZ, state, pending }] : [] }),
      "POST /api/extensions/servers": () => { known = true; return { ...LAUZ, state }; },
      "POST /api/extensions/servers/lauz/connect": () => { pending = true; return { auth_url: AUTH }; },
    });
    renderWithApp(<RemoteServers onChanged={() => undefined} />);
    await userEvent.type(await screen.findByLabelText(t("mcp.name")), "lauz");
    await userEvent.type(screen.getByLabelText(t("mcp.url")), "https://mcp.lauz.io/mcp");
    await userEvent.click(screen.getByRole("button", { name: t("mcp.add") }));
    expect(await screen.findByText(t("mcp.state.needs_auth"))).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: t("mcp.connect") }));
    await waitFor(() => expect(tab.location.href).toBe(AUTH));
    expect(tab.opener).toBeNull();
    expect(await screen.findByText(t("mcp.waiting"))).toBeInTheDocument();

    state = "connected";                                       // the browser came back to Hokma
    pending = false;
    expect(await screen.findByText(t("mcp.state.connected"), {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.queryByText(t("mcp.waiting"))).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("mcp.revoke") })).toBeInTheDocument();
    expect(calls.some((c) => c.path.includes("/login"))).toBe(false);
  });

  it("inside the desktop app the server opens the system browser", async () => {
    setLocale(locale);
    vi.stubGlobal("__TAURI_INTERNALS__", {});
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const calls = mockApi({
      "GET /api/extensions/servers": { servers: [{ ...LAUZ, state: "needs_auth" }] },
      "POST /api/extensions/servers/lauz/connect": { auth_url: AUTH },
      "POST /api/extensions/servers/lauz/open": { opened: true },
    });
    renderWithApp(<RemoteServers onChanged={() => undefined} />);
    await userEvent.click(await screen.findByRole("button", { name: t("mcp.connect") }));
    await waitFor(() => expect(calls.find((c) => c.path.endsWith("/open"))?.body).toEqual({ url: AUTH }));
    expect(open).not.toHaveBeenCalled();
  });

  it("shows the error when the server cannot start the sign-in and closes the empty tab", async () => {
    setLocale(locale);
    const tab = fakeTab();
    mockApi({
      "GET /api/extensions/servers": { servers: [{ ...LAUZ, state: "needs_auth" }] },
      "POST /api/extensions/servers/lauz/connect": () => failure(400, "No automatic registration"),
    });
    renderWithApp(<RemoteServers onChanged={() => undefined} />);
    await userEvent.click(await screen.findByRole("button", { name: t("mcp.connect") }));
    await waitFor(() => expect(screen.getByText("No automatic registration")).toBeInTheDocument());
    expect(tab.close).toHaveBeenCalled();
  });

  it("an error state offers Retry and a server without sign-in has no buttons", async () => {
    setLocale(locale);
    mockApi({
      "GET /api/extensions/servers": { servers: [
        { ...LAUZ, state: "error", message: "HTTP 503" },
        { name: "open", url: "https://open.test/mcp", message: "", state: "connected", auth: false },
      ] },
    });
    renderWithApp(<RemoteServers onChanged={() => undefined} />);
    expect(await screen.findByRole("button", { name: t("mcp.retry") })).toBeInTheDocument();
    expect(screen.getByText("HTTP 503")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: t("mcp.revoke") })).not.toBeInTheDocument();
  });

  it("rejects hokma and http addresses before sending anything", async () => {
    setLocale(locale);
    const calls = mockApi({ "GET /api/extensions/servers": { servers: [] } });
    renderWithApp(<RemoteServers onChanged={() => undefined} />);
    await userEvent.type(await screen.findByLabelText(t("mcp.name")), "hokma");
    await userEvent.type(screen.getByLabelText(t("mcp.url")), "http://x.test/mcp");
    expect(screen.getByRole("button", { name: t("mcp.add") })).toBeDisabled();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });
  it("normalizes the name by itself and says why the button is off", async () => {
    setLocale(locale);
    mockApi({ "GET /api/extensions/servers": { servers: [] } });
    renderWithApp(<RemoteServers onChanged={() => undefined} />);
    const name = await screen.findByLabelText(t("mcp.name"));
    await userEvent.type(name, "Mi \u00d1and\u00fa Caf\u00e9");
    expect(name).toHaveValue("mi-nandu-cafe");
    expect(screen.getByRole("button", { name: t("mcp.add") })).toBeDisabled();
    expect(screen.getByText(t("mcp.why.url_empty"))).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(t("mcp.url")), "http://x.test");
    expect(screen.getByText(t("mcp.why.url_format"))).toBeInTheDocument();
    await userEvent.clear(name);
    await userEvent.type(name, "Hokma");
    expect(screen.getByText(t("mcp.why.name_reserved"))).toBeInTheDocument();
  });
  it("revokes a connected server", async () => {
    setLocale(locale);
    let state = "connected";
    mockApi({
      "GET /api/extensions/servers": () => ({ servers: [{ ...LAUZ, state }] }),
      "POST /api/extensions/servers/lauz/logout": () => { state = "needs_auth"; return { state }; },
    });
    renderWithApp(<RemoteServers onChanged={() => undefined} />);
    await userEvent.click(await screen.findByRole("button", { name: t("mcp.revoke") }));
    expect(await screen.findByText(t("mcp.state.needs_auth"))).toBeInTheDocument();
  });
});
