import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { RemoteServers } from "./RemoteServers";
import { failure, mockApi } from "./testkit";
import type { Call } from "./testkit";

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

  it("an error state offers Retry, every card can be edited and removed, and an open server has no Revoke", async () => {
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
    expect(screen.getAllByRole("button", { name: t("mcp.edit") })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: t("mcp.remove") })).toHaveLength(2);
  });

  it("a web page saved as the address shows the error and never Connected (task 3152)", async () => {
    setLocale(locale);
    const message = "That address is not an MCP server: it answered HTTP 200 text/html.";
    mockApi({
      "GET /api/extensions/servers": { servers: [
        { name: "lauz", url: "https://lauz.io/app/mcp", state: "error", message, auth: false, signed_in: false },
      ] },
    });
    renderWithApp(<RemoteServers onChanged={() => undefined} />);
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.queryByText(t("mcp.state.connected"))).not.toBeInTheDocument();
    expect(screen.getByText(t("mcp.state.error"))).toBeInTheDocument();
  });

  it("the engine refuses an address that is not MCP and the form keeps it", async () => {
    setLocale(locale);
    const message = "That address is not an MCP server: it answered HTTP 200 text/html.";
    const calls = mockApi({
      "GET /api/extensions/servers": { servers: [] },
      "POST /api/extensions/servers": () => failure(400, message),
    });
    renderWithApp(<RemoteServers onChanged={() => undefined} />);
    await userEvent.type(await screen.findByLabelText(t("mcp.name")), "lauz");
    await userEvent.type(screen.getByLabelText(t("mcp.url")), "https://lauz.io/app/mcp");
    await userEvent.click(screen.getByRole("button", { name: t("mcp.add") }));
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.getByLabelText(t("mcp.url"))).toHaveValue("https://lauz.io/app/mcp");
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });

  it("a duplicate name says it is taken, not that the name is missing", async () => {
    setLocale(locale);
    const calls = mockApi({ "GET /api/extensions/servers": { servers: [{ ...LAUZ, state: "connected" }] } });
    renderWithApp(<RemoteServers onChanged={() => undefined} />);
    await screen.findByText(t("mcp.state.connected"));
    await userEvent.type(screen.getByLabelText(t("mcp.name")), "lauz");
    await userEvent.type(screen.getByLabelText(t("mcp.url")), "https://mcp.lauz.io/mcp");
    expect(screen.getByText(t("mcp.why.name_taken", { name: "lauz" }))).toBeInTheDocument();
    expect(screen.queryByText(t("mcp.why.name_empty"))).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("mcp.add") })).toBeDisabled();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("removes a server after confirming", async () => {
    setLocale(locale);
    let known = true;
    const calls = mockApi({
      "GET /api/extensions/servers": () => ({ servers: known ? [{ ...LAUZ, state: "connected", signed_in: true }] : [] }),
      "DELETE /api/extensions/servers/lauz": () => { known = false; return { name: "lauz", removed: true }; },
    });
    renderWithApp(<RemoteServers onChanged={() => undefined} />);
    await userEvent.click(await screen.findByRole("button", { name: t("mcp.remove") }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(t("mcp.confirmRemoveBody"))).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: t("mcp.remove") }));
    await waitFor(() => expect(screen.queryByText(LAUZ.url)).not.toBeInTheDocument());
    expect(calls.filter((c) => c.method === "DELETE")).toHaveLength(1);
  });

  it("edits the address of a server", async () => {
    setLocale(locale);
    let url = "https://lauz.io/app/mcp";
    const calls = mockApi({
      "GET /api/extensions/servers": () => ({ servers: [{ ...LAUZ, url, state: url.includes("/app/") ? "error" : "needs_auth" }] }),
      "PUT /api/extensions/servers/lauz": (call: Call) => { url = (call.body as { url: string }).url; return { ...LAUZ, url, state: "needs_auth" }; },
    });
    renderWithApp(<RemoteServers onChanged={() => undefined} />);
    await userEvent.click(await screen.findByRole("button", { name: t("mcp.edit") }));
    const field = screen.getByLabelText(t("mcp.editLabel", { name: "lauz" }));
    expect(field).toHaveValue("https://lauz.io/app/mcp");
    await userEvent.clear(field);
    await userEvent.type(field, "http://x");
    expect(screen.getByRole("button", { name: t("mcp.save") })).toBeDisabled();
    await userEvent.clear(field);
    await userEvent.type(field, "https://mcp.lauz.io/mcp");
    await userEvent.click(screen.getByRole("button", { name: t("mcp.save") }));
    expect(await screen.findByText("https://mcp.lauz.io/mcp")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ url: "https://mcp.lauz.io/mcp" });
    expect(await screen.findByText(t("mcp.state.needs_auth"))).toBeInTheDocument();
  });

  it("offers Revoke whenever there is a token to revoke", async () => {
    setLocale(locale);
    mockApi({ "GET /api/extensions/servers": { servers: [{ ...LAUZ, state: "error", message: "HTTP 500", signed_in: true }] } });
    renderWithApp(<RemoteServers onChanged={() => undefined} />);
    expect(await screen.findByRole("button", { name: t("mcp.revoke") })).toBeInTheDocument();
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
