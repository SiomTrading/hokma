import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { MailCard } from "./MailCard";

const URL_GOOGLE = "https://myaccount.google.com/apppasswords";
const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

let account: string | null;
let rejects: boolean;
let calls: Array<{ method: string; body: unknown }>;

const handle = async (_input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const method = init?.method ?? "GET";
  const body = init?.body ? JSON.parse(String(init.body)) : undefined;
  calls.push({ method, body });
  if (method === "PUT") {
    if (rejects) return reply({ detail: { code: "mail.bad_credentials", message: "Gmail did not accept that app password." } }, 400);
    account = body.email;
    return reply({ connected: true, email: account });
  }
  if (method === "DELETE") {
    account = null;
    return reply({ connected: false });
  }
  return reply({ connected: account !== null, email: account, store_available: true, apppasswords_url: URL_GOOGLE });
};

beforeEach(() => {
  account = null;
  rejects = false;
  calls = [];
  clearToken();
  setToken(TEST_TOKEN);
  setLocale("en");
  vi.stubGlobal("fetch", vi.fn(handle));
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("C7-2a: Gmail card", () => {
  it("not connected: explains, links to Google and keeps Connect disabled until both fields are filled", async () => {
    renderWithApp(<MailCard />);
    const card = await screen.findByTestId("mail-card");
    expect(within(card).getByText(/2-step verification/)).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "Create password at Google" })).toHaveAttribute("href", URL_GOOGLE);
    const connect = within(card).getByRole("button", { name: "Connect Gmail" });
    expect(connect).toBeDisabled();
    await userEvent.type(card.querySelector('[data-field="email"]')!, "student@gmail.com");
    expect(connect).toBeDisabled();
    await userEvent.type(card.querySelector('[data-field="password"]')!, "abcd efgh ijkl mnop");
    expect(connect).toBeEnabled();
  });

  it("connects, shows the account, forgets the password and sends it only once", async () => {
    renderWithApp(<MailCard />);
    const card = await screen.findByTestId("mail-card");
    await userEvent.type(card.querySelector('[data-field="email"]')!, "student@gmail.com");
    await userEvent.type(card.querySelector('[data-field="password"]')!, "abcd efgh ijkl mnop");
    await userEvent.click(within(card).getByRole("button", { name: "Connect Gmail" }));
    expect(await screen.findByTestId("mail-account")).toHaveTextContent("Connected as student@gmail.com");
    expect(calls.filter((c) => c.method === "PUT")).toEqual([{ method: "PUT", body: { email: "student@gmail.com", password: "abcd efgh ijkl mnop" } }]);
    expect(document.body.textContent).not.toContain("abcd efgh");
    expect(card.querySelector('[data-field="password"]')).toBeNull();
  });

  it("shows the server's message when Gmail rejects the password and stays disconnected", async () => {
    rejects = true;
    renderWithApp(<MailCard />);
    const card = await screen.findByTestId("mail-card");
    await userEvent.type(card.querySelector('[data-field="email"]')!, "student@gmail.com");
    await userEvent.type(card.querySelector('[data-field="password"]')!, "bad");
    await userEvent.click(within(card).getByRole("button", { name: "Connect Gmail" }));
    expect(await screen.findByTestId("mail-error")).toHaveTextContent("Gmail did not accept that app password.");
    expect(card).toHaveAttribute("data-mail", "off");
  });

  it("disconnects and goes back to the form", async () => {
    account = "student@gmail.com";
    renderWithApp(<MailCard />);
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Connect Gmail" })).toBeInTheDocument());
    expect(calls.some((c) => c.method === "DELETE")).toBe(true);
  });

  it("speaks Spanish", async () => {
    setLocale("es");
    renderWithApp(<MailCard />);
    expect(await screen.findByRole("button", { name: t("mail.connect") })).toBeInTheDocument();
    expect(t("mail.connect")).not.toBe("Connect Gmail");
    expect(screen.getByRole("link", { name: t("mail.openGoogle") })).toBeInTheDocument();
  });
});
