import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { RemoteServers } from "./RemoteServers";
import { failure, mockApi } from "./testkit";

afterEach(() => vi.unstubAllGlobals());

const LAUZ = { name: "lauz", url: "https://mcp.lauz.io/mcp", message: "" };
const REDIRECT = "http://localhost:51234/callback?code=ABC&state=XYZ";

describe.each(["es", "en"] as const)("Remote MCP servers (%s)", (locale) => {
  it("adds a server, authorizes with the pasted address and shows it connected", async () => {
    setLocale(locale);
    let state = "needs_auth";
    let known = false;
    const calls = mockApi({
      "GET /api/extensions/servers": () => ({ servers: known ? [{ ...LAUZ, state }] : [] }),
      "POST /api/extensions/servers": () => { known = true; return { name: "lauz" }; },
      "POST /api/extensions/servers/lauz/login": { auth_url: "https://api.lauz.io/oauth/auth?x=1" },
      "POST /api/extensions/servers/lauz/login/code": () => { state = "connected"; return { state }; },
    });
    renderWithApp(<RemoteServers onChanged={() => undefined} />);
    await userEvent.type(await screen.findByLabelText(t("mcp.name")), "lauz");
    await userEvent.type(screen.getByLabelText(t("mcp.url")), "https://mcp.lauz.io/mcp");
    await userEvent.click(screen.getByRole("button", { name: t("mcp.add") }));
    expect(await screen.findByText(t("mcp.state.needs_auth"))).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: t("mcp.authorize") }));
    const open = await screen.findByRole("link", { name: t("mcp.modal.open") });
    expect(open).toHaveAttribute("href", "https://api.lauz.io/oauth/auth?x=1");
    await userEvent.type(screen.getByLabelText(t("mcp.modal.paste")), REDIRECT);
    await userEvent.click(screen.getByRole("button", { name: t("mcp.modal.connect") }));
    expect(await screen.findByText(t("mcp.state.connected"))).toBeInTheDocument();
    expect(calls.find((c) => c.path.endsWith("/login/code"))?.body).toEqual({ pasted: REDIRECT });
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

  it("shows the error of a bad paste inside the modal and keeps it open", async () => {
    setLocale(locale);
    mockApi({
      "GET /api/extensions/servers": { servers: [{ ...LAUZ, state: "needs_auth" }] },
      "POST /api/extensions/servers/lauz/login": { auth_url: "https://api.lauz.io/oauth/auth" },
      "POST /api/extensions/servers/lauz/login/code": () => failure(400, "The sign-in did not finish"),
    });
    renderWithApp(<RemoteServers onChanged={() => undefined} />);
    await userEvent.click(await screen.findByRole("button", { name: t("mcp.authorize") }));
    await userEvent.type(await screen.findByLabelText(t("mcp.modal.paste")), "garbage");
    await userEvent.click(screen.getByRole("button", { name: t("mcp.modal.connect") }));
    await waitFor(() => expect(screen.getByText("The sign-in did not finish")).toBeInTheDocument());
    expect(screen.getByLabelText(t("mcp.modal.paste"))).toBeInTheDocument();
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
