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

  it("says there is no connection (not 'not connected') when a token exists but GitHub cannot be reached", async () => {
    setLocale(locale);
    mockApi({ "GET /api/backup": base, "GET /api/github": { connected: false, user: null, offline: true } });
    renderWithApp(<BackupSection />);
    expect(await screen.findByTestId("github-offline")).toHaveTextContent(T("backup.github.offline"));
    expect(screen.queryByRole("button", { name: T("backup.github.connect") })).toBeNull();
  });

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
    expect(screen.getByTestId("omitted")).toHaveTextContent(`.env: ${T("backup.omitted.secret")}`);
    expect(screen.getByTestId("omitted")).not.toHaveTextContent(".env: secret");
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
      "POST /api/backup/restore": {      // the real shape of restore.prepare: objects, not strings
        status: "restart_required",
        reconnect: [{ kind: "engine", name: "Claude" }, { kind: "telegram", name: "@ana" }, { kind: "folder", name: "/home/ana/docs" }, { kind: "projects", name: "no network" }],
      },
    });
    renderWithApp(<BackupSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("backup.restore") }));
    await userEvent.click(await screen.findByRole("radio"));
    const go = screen.getByRole("button", { name: T("backup.restore.confirm") });
    expect(go).toBeDisabled();
    await userEvent.type(screen.getByLabelText(T("backup.restore.key")), " KEY123 ");
    await userEvent.click(go);
    expect(await screen.findByTestId("restore-done")).toBeInTheDocument();
    expect(screen.getByText(T("backup.restore.reconnect.engine", { name: "Claude" }))).toBeInTheDocument();
    expect(screen.getByText(T("backup.restore.reconnect.telegram", { name: "@ana" }))).toBeInTheDocument();
    expect(screen.getByText(T("backup.restore.reconnect.folder", { name: "/home/ana/docs" }))).toBeInTheDocument();
    expect(screen.getByText(T("backup.restore.reconnect.projects", { name: "no network" }))).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/backup/restore")?.body).toEqual({ name: "hokma-1.hkb", key: "KEY123" });
  });

  it("a wrong last 4 does not confirm the key: the key stays on screen and the error says why (400)", async () => {
    setLocale(locale);
    mockApi({
      "GET /api/backup": base,
      "GET /api/github": { connected: true, user: "ana" },
      "POST /api/backup/key": { key: "ABCDEFGHJKLMNPQRSTUVWXYZ" },
      "POST /api/backup/key/confirm": new Response(JSON.stringify({ detail: { code: "key_mismatch", message: T("backup.key.last4.wrong") } }), { status: 400 }),
    });
    renderWithApp(<BackupSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("backup.key.create") }));
    await userEvent.type(await screen.findByLabelText(T("backup.key.last4")), "ZZZZ");
    await userEvent.click(screen.getByRole("button", { name: T("backup.key.confirm") }));
    expect(await screen.findByRole("alert")).toHaveTextContent(T("backup.key.last4.wrong"));
    expect(screen.getByTestId("recovery-key")).toHaveTextContent("ABCDEFGHJKLMNPQRSTUVWXYZ");
    expect(screen.queryByTestId("key-status")).toBeNull();
  });

  it("a 200 with ok false (older engine) does not confirm the key either", async () => {
    setLocale(locale);
    mockApi({
      "GET /api/backup": base,
      "GET /api/github": { connected: true, user: "ana" },
      "POST /api/backup/key": { key: "ABCDEFGHJKLMNPQRSTUVWXYZ" },
      "POST /api/backup/key/confirm": { ok: false },
    });
    renderWithApp(<BackupSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("backup.key.create") }));
    await userEvent.type(await screen.findByLabelText(T("backup.key.last4")), "ZZZZ");
    await userEvent.click(screen.getByRole("button", { name: T("backup.key.confirm") }));
    expect(await screen.findByRole("alert")).toHaveTextContent(T("backup.key.last4.wrong"));
    expect(screen.getByTestId("recovery-key")).toBeInTheDocument();
    expect(screen.queryByTestId("key-status")).toBeNull();
  });

  it("shows the last backup in local time: 03:23:46 UTC is 22:23 the day before in Bogota", async () => {
    setLocale(locale);
    const before = process.env.TZ;
    process.env.TZ = "America/Bogota";
    try {
      mockApi({
        "GET /api/backup": { ...base, connected: true, key_ready: true, status: "ok", last_size: 2048, last_backup_at: "2026-11-17 03:23:46" },
        "GET /api/github": { connected: true, user: "ana" },
      });
      renderWithApp(<BackupSection />);
      const line = await screen.findByText(/2\.0 KB/);
      expect(line).toHaveTextContent(/16/);
      expect(line).toHaveTextContent(locale === "es" ? "22:23" : "10:23 PM");
      expect(line).not.toHaveTextContent(/17/);
      expect(line).not.toHaveTextContent(/03:23/);
    } finally {
      if (before === undefined) delete process.env.TZ;
      else process.env.TZ = before;
    }
  });

  it("a backup with a conflict branch or a skipped project is a warning (amber), not 'up to date'", async () => {
    setLocale(locale);
    mockApi({
      "GET /api/backup": { ...base, connected: true, key_ready: true, status: "warning", message: "went to desktop/2026-11-17", omitted: [{ path: "projects/p1", reason: "git" }] },
      "GET /api/github": { connected: true, user: "ana" },
    });
    renderWithApp(<BackupSection />);
    expect(await screen.findByTestId("backup-status")).toHaveTextContent(T("backup.status.warning"));
    expect(screen.getByTestId("backup-message")).toHaveTextContent("desktop/2026-11-17");
    expect(screen.getByTestId("omitted")).toHaveTextContent(`projects/p1: ${T("backup.omitted.git")}`);
  });

  it("stops waiting for the authorization after 3 failed checks in a row and offers Connect GitHub again", async () => {
    setLocale(locale);
    let checks = 0;
    mockApi({
      "GET /api/backup": base,
      "GET /api/github": off,
      "POST /api/github/connect": { user_code: "AB12-CD34", verification_uri: "https://github.com/login/device", interval: 0.01 },
      "GET /api/github/connect": () => {
        checks += 1;
        return new Response(JSON.stringify({ detail: { code: "github.unreachable", message: "GitHub down" } }), { status: 502 });
      },
    });
    renderWithApp(<BackupSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("backup.github.connect") }));
    expect(await screen.findByRole("alert")).toHaveTextContent("GitHub down");
    expect(screen.queryByTestId("device-code")).toBeNull();
    expect(screen.queryByText(T("backup.github.waiting"))).toBeNull();
    expect(screen.getByRole("button", { name: T("backup.github.connect") })).toBeInTheDocument();
    expect(checks).toBe(3);
  });

  it("stops at once on a 503 (no credential store): it will not fix itself", async () => {
    setLocale(locale);
    let checks = 0;
    mockApi({
      "GET /api/backup": base,
      "GET /api/github": off,
      "POST /api/github/connect": { user_code: "AB12-CD34", verification_uri: "https://github.com/login/device", interval: 0.01 },
      "GET /api/github/connect": () => {
        checks += 1;
        return new Response(JSON.stringify({ detail: { code: "github.store_unavailable", message: "no keychain" } }), { status: 503 });
      },
    });
    renderWithApp(<BackupSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("backup.github.connect") }));
    expect(await screen.findByRole("alert")).toHaveTextContent("no keychain");
    expect(checks).toBe(1);
  });

  it("one failed check followed by a good one keeps waiting (the failures must be in a row)", async () => {
    setLocale(locale);
    let checks = 0;
    mockApi({
      "GET /api/backup": base,
      "GET /api/github": off,
      "POST /api/github/connect": { user_code: "AB12-CD34", verification_uri: "https://github.com/login/device", interval: 0.01 },
      "GET /api/github/connect": () => {
        checks += 1;
        if (checks % 2 === 1 && checks < 7) return new Response(JSON.stringify({ detail: { code: "github.unreachable", message: "blip" } }), { status: 502 });
        return checks < 8 ? { status: "pending" } : { status: "connected", user: "ana" };
      },
    });
    renderWithApp(<BackupSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("backup.github.connect") }));
    expect(await screen.findByTestId("github-status", undefined, { timeout: 3000 })).toHaveTextContent(T("backup.github.connected", { user: "ana" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("after a reload in the middle of the flow, the code the engine still waits for is shown again", async () => {
    setLocale(locale);
    mockApi({
      "GET /api/backup": base,
      "GET /api/github": { connected: false, user: null, offline: false, pending: { user_code: "ZZ99-YY88", verification_uri: "https://github.com/login/device", interval: 0.01, expires_in: 800 } },
      "GET /api/github/connect": { status: "pending" },
    });
    renderWithApp(<BackupSection />);
    expect(await screen.findByTestId("device-code")).toHaveTextContent("ZZ99-YY88");
    expect(screen.getByText(T("backup.github.waiting"))).toBeInTheDocument();
  });

  it("a backup that failed because GitHub dropped the token takes the 'Connected as' pill down", async () => {
    setLocale(locale);
    let githubReads = 0;
    mockApi({
      "GET /api/backup": { ...base, connected: true, key_ready: true, status: "error", message: "connect GitHub", code: "github.not_connected" },
      "GET /api/github": () => (++githubReads < 2 ? { connected: true, user: "ana" } : { connected: false, user: null, offline: false, pending: null }),
    });
    renderWithApp(<BackupSection />);
    expect(await screen.findByRole("button", { name: T("backup.github.connect") })).toBeInTheDocument();
    expect(screen.queryByTestId("github-status")).toBeNull();
    expect(githubReads).toBeGreaterThanOrEqual(2);
  });

  it("explains a restore that could not be applied and lets the student dismiss it", async () => {
    setLocale(locale);
    let dismissed = false;
    const calls = mockApi({
      "GET /api/backup": () => ({ ...base, connected: true, key_ready: true, restore_failed: dismissed ? null : { at: "2026-11-17 03:23:46" } }),
      "GET /api/github": { connected: true, user: "ana" },
      "DELETE /api/backup/restore-failed": () => { dismissed = true; return { ok: true }; },
    });
    renderWithApp(<BackupSection />);
    expect(await screen.findByTestId("restore-failed")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: T("backup.restore.failed.dismiss") }));
    await waitFor(() => expect(screen.queryByTestId("restore-failed")).toBeNull());
    expect(calls.some((c) => c.method === "DELETE" && c.path === "/api/backup/restore-failed")).toBe(true);
  });

  it("says what the connection permission covers before connecting", async () => {
    setLocale(locale);
    mockApi({ "GET /api/backup": base, "GET /api/github": off });
    renderWithApp(<BackupSection />);
    expect(await screen.findByText(T("backup.github.scope"))).toBeInTheDocument();
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
