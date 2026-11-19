import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { mockApi } from "../system/testkit";
import { MailSection } from "./MailSection";

afterEach(() => vi.unstubAllGlobals());

const on = { connected: true, email: "ana@gmail.com", fallback: false, jobs: [{ id: 7, name: "Briefing", mail: false }] };

describe.each(["es", "en"] as const)("MailSection (%s)", (locale) => {
  const T = (key: string, values?: Record<string, string>) => t(key, values, locale);

  it("without Gmail explains and offers to connect", async () => {
    setLocale(locale);
    mockApi({ "GET /api/mail/notify": { ...on, connected: false, email: null } });
    renderWithApp(<MailSection />);
    expect(await screen.findByText(T("mailNotify.notConnected"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: T("mailNotify.openConnections") })).toBeInTheDocument();
  });

  it("without routines says so and offers Tasks", async () => {
    setLocale(locale);
    mockApi({ "GET /api/mail/notify": { ...on, jobs: [] } });
    renderWithApp(<MailSection />);
    expect(await screen.findByText(T("mailNotify.noRoutines"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: T("mailNotify.openTasks") })).toBeInTheDocument();
  });

  it("saves the routine choice and the Telegram fallback", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/mail/notify": on,
      "PUT /api/mail/notify": (call: { body: any }) => ({ ...on, fallback: call.body.fallback, jobs: [{ id: 7, name: "Briefing", mail: call.body.jobs.includes(7) }] }),
    });
    renderWithApp(<MailSection />);
    await userEvent.click(await screen.findByRole("switch", { name: T("mailNotify.routine", { name: "Briefing" }) }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ fallback: false, jobs: [7] }));
    await userEvent.click(await screen.findByRole("switch", { name: T("mailNotify.fallback") }));
    await waitFor(() => expect(calls.filter((c) => c.method === "PUT").at(-1)?.body).toEqual({ fallback: true, jobs: [7] }));
  });

  it("sends the test mail and says where it went", async () => {
    setLocale(locale);
    mockApi({ "GET /api/mail/notify": on, "POST /api/mail/notify/test": { sent: true } });
    renderWithApp(<MailSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("mailNotify.test") }));
    expect(await screen.findByText(T("mailNotify.testSent", { email: "ana@gmail.com" }))).toBeInTheDocument();
  });

  it("shows the error when the test mail fails", async () => {
    setLocale(locale);
    mockApi({ "GET /api/mail/notify": on, "POST /api/mail/notify/test": () => new Response(JSON.stringify({ detail: { code: "mail.unreachable", message: "Cannot reach Gmail." } }), { status: 502 }) });
    renderWithApp(<MailSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("mailNotify.test") }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach Gmail.");
  });
});
