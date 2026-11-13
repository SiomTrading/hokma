import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { mockApi } from "../system/testkit";
import { BackupSection, sizeLabel } from "./BackupSection";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const base = {
  connected: false, repo: null, last_backup_at: null, last_size: null, status: "idle", message: "",
  total_size_estimate: 5 * 1024 ** 3, omitted: [], key_ready: false,
};
const off = { connected: false, user: null };

describe("sizeLabel", () => {
  it("shows gigabytes for big backups", () => {
    expect(sizeLabel(3 * 1024 ** 3)).toBe("3.0 GB");
    expect(sizeLabel(2048)).toBe("2.0 KB");
  });
});

describe.each(["es", "en"] as const)("BackupSection (%s)", (locale) => {
  const T = (key: string, params?: Record<string, string | number>) => t(key, params, locale);

  it("connects GitHub: shows the code, polls until connected, then disconnects", async () => {
    setLocale(locale);
    let checks = 0;
    const calls = mockApi({
      "GET /api/backup": base,
      "GET /api/github": off,
      "POST /api/github/connect": { user_code: "AB12-CD34", verification_uri: "https://github.com/login/device", interval: 0.01 },
      "GET /api/github/connect": () => (++checks < 2 ? { status: "pending" } : { status: "connected", user: "ana" }),
      "DELETE /api/github": { connected: false },
    });
    renderWithApp(<BackupSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("backup.github.connect") }));
    expect(await screen.findByTestId("device-code")).toHaveTextContent("AB12-CD34");
    expect(screen.getByRole("link", { name: T("backup.github.open") })).toHaveAttribute("href", "https://github.com/login/device");
    expect(await screen.findByTestId("github-status")).toHaveTextContent(T("backup.github.connected", { user: "ana" }));
    expect(checks).toBe(2);
    await userEvent.click(screen.getByRole("button", { name: T("backup.github.disconnect") }));
    expect(await screen.findByRole("button", { name: T("backup.github.connect") })).toBeInTheDocument();
    expect(calls.some((c) => c.method === "DELETE" && c.path === "/api/github")).toBe(true);
  });

  it("shows the recovery key once and confirms it with the last 4 characters", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/backup": base,
      "GET /api/github": { connected: true, user: "ana" },
      "POST /api/backup/key": { key: "ABCDEFGHJKLMNPQRSTUVWXYZ" },
      "POST /api/backup/key/confirm": { ok: true },
    });
    renderWithApp(<BackupSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("backup.key.create") }));
    expect(await screen.findByTestId("recovery-key")).toHaveTextContent("ABCDEFGHJKLMNPQRSTUVWXYZ");
    const confirm = screen.getByRole("button", { name: T("backup.key.confirm") });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText(T("backup.key.last4")), "WXYZ");
    await userEvent.click(confirm);
    expect(await screen.findByTestId("key-status")).toBeInTheDocument();
    expect(screen.queryByTestId("recovery-key")).not.toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/backup/key/confirm")?.body).toEqual({ last4: "WXYZ" });
  });

  it("shows state, size, omitted files and the responsibility line; Back up now polls until done", async () => {
    setLocale(locale);
    let reads = 0;
    const calls = mockApi({
      "GET /api/backup": () => (++reads < 2
        ? { ...base, connected: true, key_ready: true, last_backup_at: "2026-11-01T10:00:00Z", last_size: 2048, status: "ok", omitted: [{ path: ".env", reason: "secret" }] }
        : { ...base, connected: true, key_ready: true, status: "ok" }),
      "GET /api/github": { connected: true, user: "ana" },
      "POST /api/backup/run": { status: "running" },
    });
    renderWithApp(<BackupSection />);
    expect(await screen.findByTestId("backup-status")).toHaveTextContent(T("backup.status.ok"));
    expect(screen.getByTestId("omitted")).toHaveTextContent(".env: secret");
    expect(screen.getByText(T("backup.estimate", { size: "5.0 GB" }))).toBeInTheDocument();
    expect(screen.getByText(T("backup.responsibility"))).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: T("backup.now") }));
    expect(screen.getByTestId("backup-status")).toHaveTextContent(T("backup.status.running"));
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/backup/run")).toBe(true);
    await waitFor(() => expect(screen.getByTestId("backup-status")).toHaveTextContent(T("backup.status.ok")), { timeout: 4000 });
  });

  it("restores: lists backups, needs the key, shows what to reconnect", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/backup": { ...base, connected: true, key_ready: true },
      "GET /api/github": { connected: true, user: "ana" },
      "GET /api/backup/list": [{ name: "hokma-1.hkb", size: 1024 ** 2, created_at: "2026-11-01T10:00:00Z" }],
      "POST /api/backup/restore": { status: "prepared", reconnect: ["Telegram"] },
    });
    renderWithApp(<BackupSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("backup.restore") }));
    await userEvent.click(await screen.findByRole("radio"));
    const go = screen.getByRole("button", { name: T("backup.restore.confirm") });
    expect(go).toBeDisabled();
    await userEvent.type(screen.getByLabelText(T("backup.restore.key")), " KEY123 ");
    await userEvent.click(go);
    expect(await screen.findByTestId("restore-done")).toBeInTheDocument();
    expect(screen.getByText("Telegram")).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/backup/restore")?.body).toEqual({ name: "hokma-1.hkb", key: "KEY123" });
  });

  it("shows the API error in plain language when the key is wrong", async () => {
    setLocale(locale);
    mockApi({
      "GET /api/backup": { ...base, connected: true, key_ready: true },
      "GET /api/github": { connected: true, user: "ana" },
      "GET /api/backup/list": [{ name: "a.hkb", size: 10, created_at: "2026-11-01T10:00:00Z" }],
      "POST /api/backup/restore": new Response(JSON.stringify({ detail: { code: "bad_key", message: "Wrong key" } }), { status: 400 }),
    });
    renderWithApp(<BackupSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("backup.restore") }));
    await userEvent.click(await screen.findByRole("radio"));
    await userEvent.type(screen.getByLabelText(T("backup.restore.key")), "x");
    await userEvent.click(screen.getByRole("button", { name: T("backup.restore.confirm") }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Wrong key");
  });
});
