import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, setToken } from "@/api";
import { getCatalog, setLocale, t } from "@/i18n";
import { ChatScreen } from "@/screens/chat/ChatScreen";
import { FakeServer } from "@/screens/chat/testkit";
import { setHub } from "@/screens/chat/hub";
import { PermissionsSettings, ProtectionsList } from "./PermissionsSettings";
import { PermissionPill } from "./PermissionPill";
import { IntroNotice } from "./IntroNotice";
import { TrashSection } from "./TrashSection";
import { permissionErrorText } from "./api";
import { resetModeStore } from "./store";
import { RULES } from "./types";
import { ShellProvider } from "@/shell/store";
import { useShell } from "@/shell/useShell";

interface Call {
  method: string;
  path: string;
  body: unknown;
}

const RULE_IDS = [...RULES];

/** A stand-in for the routes of this delivery. It remembers what was sent and answers like the real ones. */
class FakeBackend {
  calls: Call[] = [];
  permission = { mode: "ask", intro_seen: true };
  turnsRunning = 0;
  trash = {
    items: [
      { id: 4, name: "viejo.md", original_path: "/work/informes/viejo.md", project_id: 1, project_name: "Main", conversation_id: 7, conversation_title: "Plan", size_bytes: 2048, moved_at: "2026-11-05T12:30:00Z", reason: "obsolete draft", is_dir: false, present: true },
      { id: 5, name: "old-data", original_path: "/work/old-data", project_id: 1, project_name: "Main", conversation_id: null, conversation_title: null, size_bytes: 1048576, moved_at: "2026-11-04T09:00:00Z", reason: "old data", is_dir: true, present: true },
    ],
    count: 2,
    total_bytes: 1050624,
  };
  noGit = false;
  changes = { id: "20261105T120000Z-abcd", deleted: ["src/a.py", "docs/b.md"], changed: ["README.md"], truncated: false };

  json(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  }

  handle = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    this.calls.push({ method, path: url.pathname + url.search, body });
    const route = `${method} ${url.pathname}`;
    if (route === "GET /api/profile/permission-mode") return this.json(this.permission);
    if (route === "PUT /api/profile/permission-mode") {
      const b = body as { mode?: string; intro_seen?: boolean };
      const before = this.permission.mode;
      if (b.mode) this.permission = { mode: b.mode, intro_seen: true };
      else if (b.intro_seen !== undefined) this.permission = { ...this.permission, intro_seen: b.intro_seen };
      return this.json({ ...this.permission, changed: before !== this.permission.mode, turns_running: this.turnsRunning });
    }
    if (route === "GET /api/protections") {
      return this.json({ always_on: true, rules: RULE_IDS.map((id) => ({ id, title: `server ${id}` })) });
    }
    if (route === "GET /api/trash") return this.json(this.trash);
    if (route === "POST /api/trash/empty") {
      if (!(body as { confirm?: boolean }).confirm) return this.json({ detail: { code: "confirm_required", message: "x" } }, 400);
      const count = this.trash.count;
      this.trash = { items: [], count: 0, total_bytes: 0 };
      return this.json({ count });
    }
    let m = /^POST \/api\/trash\/(\d+)\/restore$/.exec(route);
    if (m) {
      this.trash = { ...this.trash, items: this.trash.items.filter((i) => i.id !== Number(m![1])) };
      return this.json({ id: Number(m[1]), restored_to: "/work/informes/viejo.md", renamed: false });
    }
    m = /^POST \/api\/trash\/(\d+)\/purge$/.exec(route);
    if (m) {
      if (!(body as { confirm?: boolean }).confirm) return this.json({ detail: { code: "confirm_required", message: "x" } }, 400);
      this.trash = { ...this.trash, items: this.trash.items.filter((i) => i.id !== Number(m![1])) };
      return this.json({ id: Number(m[1]) });
    }
    if (route === "GET /api/projects") {
      return this.json({ workdir: "/work", root: "/work", active_id: 1, kinds: [], projects: [{ id: 1, name: "Main", slug: "main", kind: "general", path: "/work", active: true, exists: true, files: 3, git: {} }] });
    }
    if (route === "GET /api/checkpoints") {
      if (this.noGit) return this.json({ detail: { code: "no_git", message: "no git" } }, 409);
      return this.json({ project: { id: "1", name: "Main" }, points: [{ id: "20261105T120000Z-abcd", at: "2026-11-05T12:00:00Z", conversation_id: "7" }] });
    }
    if (/^GET \/api\/checkpoints\/[^/]+\/changes$/.test(route)) return this.json(this.changes);
    if (/^POST \/api\/checkpoints\/[^/]+\/restore-file$/.test(route)) {
      const path = (body as { path: string }).path;
      this.changes = { ...this.changes, deleted: this.changes.deleted.filter((p) => p !== path), changed: this.changes.changed.filter((p) => p !== path) };
      return this.json({ restored: [path], renamed: [] });
    }
    if (/^POST \/api\/checkpoints\/[^/]+\/restore-deleted$/.test(route)) {
      const count = this.changes.deleted.length;
      this.changes = { ...this.changes, deleted: [] };
      return this.json({ restored: [], count });
    }
    if (url.pathname.startsWith("/api/chat/options")) return this.json({ agents: [], projects: [], default_project: { id: "default", name: "Main" } });
    if (url.pathname.startsWith("/api/conversations")) return this.json({ conversations: [] });
    return this.json({}, 200);
  };

  sent(method: string, path: string): Call[] {
    return this.calls.filter((c) => c.method === method && c.path.split("?")[0] === path);
  }
}

let backend: FakeBackend;

beforeEach(() => {
  backend = new FakeBackend();
  vi.stubGlobal("fetch", vi.fn(backend.handle));
  setToken("test-session-token-0123456789");
  setLocale("en");
  resetModeStore();
});

afterEach(() => {
  setHub(null);
  vi.unstubAllGlobals();
});

function Harness({ children }: { children: React.ReactNode }) {
  return (
    <ShellProvider>
      <div className="hk">{children}</div>
    </ShellProvider>
  );
}

function SettingsProbe() {
  const shell = useShell();
  return <span data-testid="settings">{shell.settings.open ? shell.settings.section : "closed"}</span>;
}

function Toasts() {
  const shell = useShell();
  return (
    <ul aria-label="toasts">
      {shell.toasts.map((toast) => (
        <li key={toast.id}>{toast.text}</li>
      ))}
    </ul>
  );
}

describe("permission pill", () => {
  it("shows the current mode and a menu with the two modes", async () => {
    const user = userEvent.setup();
    render(
      <Harness>
        <PermissionPill running={false} />
      </Harness>,
    );
    const pill = await screen.findByTestId("permission-pill");
    expect(pill).toHaveTextContent(t("permissions.pill.ask"));
    await user.click(pill);
    const menu = screen.getByRole("menu", { name: t("permissions.menu") });
    expect(within(menu).getAllByRole("menuitem")).toHaveLength(2);
    expect(within(menu).getByRole("menuitem", { name: new RegExp(t("permissions.mode.ask")) })).toHaveClass("on");
  });

  it("when the first-time modal was already seen, turning on 'no asking' is one click and no modal", async () => {
    const user = userEvent.setup();
    render(
      <Harness>
        <PermissionPill running={false} />
      </Harness>,
    );
    await user.click(await screen.findByTestId("permission-pill"));
    await user.click(screen.getByRole("menuitem", { name: new RegExp(t("permissions.mode.auto")) }));
    await waitFor(() => expect(screen.getByTestId("permission-pill")).toHaveAttribute("data-mode", "auto"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(backend.sent("PUT", "/api/profile/permission-mode")[0]!.body).toEqual({ mode: "auto" });
  });

  it("the first time, turning on 'no asking' goes through a short modal with the three promises", async () => {
    backend.permission = { mode: "ask", intro_seen: false };
    const user = userEvent.setup();
    render(
      <Harness>
        <PermissionPill running={false} />
      </Harness>,
    );
    await user.click(await screen.findByTestId("permission-pill"));
    await user.click(screen.getByRole("menuitem", { name: new RegExp(t("permissions.mode.auto")) }));
    const dialog = await screen.findByRole("dialog", { name: t("permissions.modal.title") });
    expect(dialog).toHaveTextContent(t("permissions.modal.body"));
    expect(backend.sent("PUT", "/api/profile/permission-mode")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: t("permissions.modal.confirm") }));
    await waitFor(() => expect(screen.getByTestId("permission-pill")).toHaveAttribute("data-mode", "auto"));
    expect(backend.sent("PUT", "/api/profile/permission-mode")[0]!.body).toEqual({ mode: "auto" });

    // from now on, switching is immediate: no modal
    await user.click(screen.getByTestId("permission-pill"));
    await user.click(screen.getByRole("menuitem", { name: new RegExp(t("permissions.mode.ask")) }));
    await waitFor(() => expect(screen.getByTestId("permission-pill")).toHaveAttribute("data-mode", "ask"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(backend.sent("PUT", "/api/profile/permission-mode")[1]!.body).toEqual({ mode: "ask" });
  });

  it("with a turn running it says the change applies when the turn ends, and stops saying it after", async () => {
    const user = userEvent.setup();
    backend.permission = { mode: "auto", intro_seen: true };
    backend.turnsRunning = 1;
    const view = render(
      <Harness>
        <PermissionPill running />
      </Harness>,
    );
    await user.click(await screen.findByTestId("permission-pill"));
    await user.click(screen.getByRole("menuitem", { name: new RegExp(t("permissions.mode.ask")) }));
    expect(await screen.findByText(t("permissions.pending"))).toBeInTheDocument();
    view.rerender(
      <Harness>
        <PermissionPill running={false} />
      </Harness>,
    );
    await waitFor(() => expect(screen.queryByText(t("permissions.pending"))).not.toBeInTheDocument());
  });

  it("shows the translated message when the server refuses the change", async () => {
    const user = userEvent.setup();
    backend.permission = { mode: "auto", intro_seen: true };
    const original = backend.handle;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
        init?.method === "PUT" ? backend.json({ detail: { code: "bad_mode", message: "server text" } }, 400) : original(input, init),
      ),
    );
    render(
      <Harness>
        <PermissionPill running={false} />
        <Toasts />
      </Harness>,
    );
    await user.click(await screen.findByTestId("permission-pill"));
    await user.click(screen.getByRole("menuitem", { name: new RegExp(t("permissions.mode.ask")) }));
    expect(await screen.findByText(t("permissions.err.bad_mode"))).toBeInTheDocument();
    expect(screen.getByTestId("permission-pill")).toHaveAttribute("data-mode", "auto");
  });
});

describe("one-time notice for an updated installation", () => {
  it("offers 'Turn on', which goes through the modal and then hides the notice", async () => {
    backend.permission = { mode: "ask", intro_seen: false };
    const user = userEvent.setup();
    render(
      <Harness>
        <IntroNotice />
      </Harness>,
    );
    const notice = await screen.findByTestId("permission-notice");
    expect(notice).toHaveTextContent(t("permissions.notice.title"));
    await user.click(within(notice).getByRole("button", { name: t("permissions.notice.turnOn") }));
    const dialog = await screen.findByRole("dialog", { name: t("permissions.modal.title") });
    await user.click(within(dialog).getByRole("button", { name: t("permissions.modal.confirm") }));
    await waitFor(() => expect(screen.queryByTestId("permission-notice")).not.toBeInTheDocument());
    expect(backend.permission).toEqual({ mode: "auto", intro_seen: true });
  });

  it("'Not now' keeps the mode and never shows the notice again", async () => {
    backend.permission = { mode: "ask", intro_seen: false };
    const user = userEvent.setup();
    render(
      <Harness>
        <IntroNotice />
      </Harness>,
    );
    await user.click(await screen.findByRole("button", { name: t("permissions.notice.later") }));
    await waitFor(() => expect(screen.queryByTestId("permission-notice")).not.toBeInTheDocument());
    expect(backend.sent("PUT", "/api/profile/permission-mode")[0]!.body).toEqual({ intro_seen: true });
    expect(backend.permission.mode).toBe("ask");
  });

  it("is not shown to an installation that already chose or that starts in 'no asking'", async () => {
    backend.permission = { mode: "auto", intro_seen: true };
    render(
      <Harness>
        <IntroNotice />
      </Harness>,
    );
    await waitFor(() => expect(backend.sent("GET", "/api/profile/permission-mode")).toHaveLength(1));
    expect(screen.queryByTestId("permission-notice")).not.toBeInTheDocument();
  });
});

describe("Settings > General > Permissions", () => {
  it("lists the always-on protections with no switch for any of them", async () => {
    render(
      <Harness>
        <ProtectionsList />
      </Harness>,
    );
    const list = await screen.findByTestId("protections");
    expect(within(list).getAllByRole("listitem")).toHaveLength(7);
    for (const id of RULE_IDS) expect(within(list).getByText(t(`protections.${id}`))).toBeInTheDocument();
    expect(within(list).queryByRole("switch")).not.toBeInTheDocument();
    expect(within(list).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(within(list).queryByRole("button")).not.toBeInTheDocument();
    expect(list.querySelector("input,button,select")).toBeNull();
  });

  it("has a single switch, the mode, and the mode switch is in one click", async () => {
    const user = userEvent.setup();
    backend.permission = { mode: "auto", intro_seen: true };
    render(
      <Harness>
        <PermissionsSettings />
      </Harness>,
    );
    await screen.findByTestId("protections");
    const switches = screen.getAllByRole("switch");
    expect(switches).toHaveLength(1);
    expect(switches[0]).toHaveAttribute("aria-label", t("permissions.mode.auto"));
    expect(switches[0]).toHaveAttribute("aria-checked", "true");
    await user.click(switches[0]!);
    await waitFor(() => expect(switches[0]).toHaveAttribute("aria-checked", "false"));
    expect(backend.permission.mode).toBe("ask");
  });
});

describe("chat: pill and Blocked card", () => {
  const screenProps = { tabKey: "chat", params: {}, active: true };
  const base = { conversation_id: "7", turn_id: "t1" };
  const stored = { tabKey: "chat:7", params: { conversation: "7" }, active: true };
  let server: FakeServer;

  async function mount(props = screenProps) {
    server = new FakeServer();
    render(
      <Harness>
        <ChatScreen {...props} />
        <SettingsProbe />
      </Harness>,
    );
    server.open();
    await screen.findByTestId("permission-pill");
  }

  it("puts the pill in the composer next to the model selector", async () => {
    await mount();
    const pill = screen.getByTestId("permission-pill");
    const model = screen.getByRole("button", { name: new RegExp(t("chat.model.sonnet")) });
    expect(pill.parentElement).toBe(model.parentElement);
  });

  it("shows a Blocked card live with the rule, what it tried and the reason, and goes to the Trash", async () => {
    const user = userEvent.setup();
    await mount(stored);
    server.emit({ type: "guard_block", ...base, id: 11, rule: "delete", tool: "Bash", target: "rm -rf informes", at: "2026-11-05 12:00:00" });
    const card = screen.getByTestId("block-card");
    expect(card).toHaveTextContent(t("block.title.delete"));
    expect(card).toHaveTextContent(t("block.wanted", { target: "rm -rf informes" }));
    expect(card).toHaveTextContent(t("block.why.delete"));
    server.emit({ type: "guard_block", ...base, id: 11, rule: "delete", tool: "Bash", target: "rm -rf informes", at: "2026-11-05 12:00:00" });
    expect(screen.getAllByTestId("block-card")).toHaveLength(1); // the same event twice is one card
    await user.click(within(card).getByRole("button", { name: t("block.viewTrash") }));
    expect(screen.getByTestId("settings")).toHaveTextContent("trash");
  });

  it("every rule has its own title and reason, and only 'delete' offers the Trash", async () => {
    await mount(stored);
    RULE_IDS.forEach((rule, index) => server.emit({ type: "guard_block", ...base, id: 100 + index, rule, tool: "Bash", target: `target ${rule}`, at: "2026-11-05 12:00:00" }));
    const cards = screen.getAllByTestId("block-card");
    expect(cards).toHaveLength(7);
    cards.forEach((card, index) => {
      const rule = RULE_IDS[index]!;
      expect(card).toHaveTextContent(t(`block.title.${rule}`));
      expect(card).toHaveTextContent(t(`block.why.${rule}`));
      expect(within(card).queryByRole("button", { name: t("block.viewTrash") })).toBe(rule === "delete" ? within(card).getByRole("button") : null);
    });
  });

  it("is still there when the conversation is opened again (blocks travel with the saved answer)", async () => {
    await mount(stored);
    server.emit({
      type: "history",
      conversation_id: "7",
      messages: [
        { id: "1", role: "user", text: "delete notes", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 1 },
        {
          id: "2", role: "assistant", text: "I moved it to the Trash.", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 2,
          blocks: [{ id: 9, rule: "delete", tool: "Bash", target: "rm notes.md", at: "2026-11-05 12:00:00" }],
        },
      ],
    });
    const card = await screen.findByTestId("block-card");
    expect(card).toHaveTextContent(t("block.title.delete"));
    expect(card).toHaveTextContent("rm notes.md");
    expect(screen.getByText("I moved it to the Trash.")).toBeInTheDocument();
  });

  it("shows the one-time notice in the chat for an updated installation", async () => {
    backend.permission = { mode: "ask", intro_seen: false };
    await mount();
    expect(await screen.findByTestId("permission-notice")).toHaveTextContent(t("permissions.notice.title"));
  });
});

describe("Settings > Trash", () => {
  it("lists name, original path, project, conversation, size, date and reason, with the total size", async () => {
    render(
      <Harness>
        <TrashSection />
      </Harness>,
    );
    const rows = await screen.findAllByTestId("trash-item");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("viejo.md");
    expect(rows[0]).toHaveTextContent("/work/informes/viejo.md");
    expect(rows[0]).toHaveTextContent(t("trash.project", { name: "Main" }));
    expect(rows[0]).toHaveTextContent(t("trash.conversation", { title: "Plan" }));
    expect(rows[0]).toHaveTextContent("2 KB");
    expect(rows[0]).toHaveTextContent(t("trash.reason", { reason: "obsolete draft" }));
    expect(rows[1]).toHaveTextContent("1.0 MB");
    expect(screen.getByTestId("trash-total")).toHaveTextContent(t("trash.total", { count: 2, size: "1.0 MB" }));
  });

  it("says '1 item' in the singular", async () => {
    backend.trash = { ...backend.trash, items: backend.trash.items.slice(0, 1), count: 1, total_bytes: 2048 };
    render(
      <Harness>
        <TrashSection />
      </Harness>,
    );
    await screen.findAllByTestId("trash-item");
    expect(screen.getByTestId("trash-total")).toHaveTextContent(t("trash.total.one", { size: "2 KB" }));
    expect(screen.getByTestId("trash-total")).not.toHaveTextContent("1 items");
    expect(screen.getByTestId("trash-total")).not.toHaveTextContent("1 elementos");
  });

  it("restores an item with one click", async () => {
    const user = userEvent.setup();
    render(
      <Harness>
        <TrashSection />
        <Toasts />
      </Harness>,
    );
    const rows = await screen.findAllByTestId("trash-item");
    await user.click(within(rows[0]!).getByRole("button", { name: t("trash.restore") }));
    expect(await screen.findByText(t("trash.restored", { path: "/work/informes/viejo.md" }))).toBeInTheDocument();
    expect(backend.sent("POST", "/api/trash/4/restore")).toHaveLength(1);
    await waitFor(() => expect(screen.getAllByTestId("trash-item")).toHaveLength(1));
  });

  it("'Delete for good' asks the owner first and sends the confirmation", async () => {
    const user = userEvent.setup();
    render(
      <Harness>
        <TrashSection />
      </Harness>,
    );
    const rows = await screen.findAllByTestId("trash-item");
    await user.click(within(rows[0]!).getByRole("button", { name: t("trash.purge") }));
    const dialog = await screen.findByRole("dialog", { name: t("trash.purge.title") });
    expect(backend.sent("POST", "/api/trash/4/purge")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: t("trash.cancel") }));
    expect(backend.sent("POST", "/api/trash/4/purge")).toHaveLength(0);

    await user.click(within(screen.getAllByTestId("trash-item")[0]!).getByRole("button", { name: t("trash.purge") }));
    await user.click(within(await screen.findByRole("dialog", { name: t("trash.purge.title") })).getByRole("button", { name: t("trash.purge.confirm") }));
    await waitFor(() => expect(backend.sent("POST", "/api/trash/4/purge")).toHaveLength(1));
    expect(backend.sent("POST", "/api/trash/4/purge")[0]!.body).toEqual({ confirm: true });
  });

  it("'Empty trash' asks the owner first and sends the confirmation", async () => {
    const user = userEvent.setup();
    render(
      <Harness>
        <TrashSection />
      </Harness>,
    );
    await screen.findAllByTestId("trash-item");
    await user.click(screen.getByRole("button", { name: t("trash.empty.button") }));
    const dialog = await screen.findByRole("dialog", { name: t("trash.empty.title") });
    expect(backend.sent("POST", "/api/trash/empty")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: t("trash.empty.confirm") }));
    await waitFor(() => expect(backend.sent("POST", "/api/trash/empty")).toHaveLength(1));
    expect(backend.sent("POST", "/api/trash/empty")[0]!.body).toEqual({ confirm: true });
    expect(await screen.findByText(t("trash.empty.none"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("trash.empty.button") })).toBeDisabled();
  });

  it("restore points: view changes, restore one file, restore all deleted", async () => {
    const user = userEvent.setup();
    render(
      <Harness>
        <TrashSection />
        <Toasts />
      </Harness>,
    );
    const point = await screen.findByTestId("restore-point");
    expect(point).toHaveTextContent(t("points.conversation", { id: "7" }));
    await user.click(within(point).getByRole("button", { name: t("points.viewChanges") }));
    const changes = await screen.findByTestId("point-changes");
    expect(changes).toHaveTextContent("src/a.py");
    expect(changes).toHaveTextContent("README.md");
    expect(backend.sent("GET", "/api/checkpoints/20261105T120000Z-abcd/changes")).toHaveLength(1);
    expect(backend.calls.find((c) => c.path.includes("/changes"))!.path).toContain("project_id=1");

    await user.click(within(changes).getAllByRole("button", { name: t("points.restoreFile") })[0]!);
    expect(await screen.findByText(t("points.fileRestored", { path: "src/a.py" }))).toBeInTheDocument();
    expect(backend.sent("POST", "/api/checkpoints/20261105T120000Z-abcd/restore-file")[0]!.body).toEqual({ path: "src/a.py", project_id: "1" });

    await user.click(await screen.findByRole("button", { name: t("points.restoreAll") }));
    expect(await screen.findByText(t("points.allRestored", { count: 1 }))).toBeInTheDocument();
    expect(backend.sent("POST", "/api/checkpoints/20261105T120000Z-abcd/restore-deleted")[0]!.body).toEqual({ project_id: "1" });
  });

  it("restore points: a folder without Git shows the catalog message, not a broken list", async () => {
    backend.noGit = true;
    render(
      <Harness>
        <TrashSection />
      </Harness>,
    );
    expect(await screen.findByTestId("points-no-git")).toHaveTextContent(t("permissions.err.no_git"));
    expect(screen.queryByTestId("restore-point")).not.toBeInTheDocument();
  });
});

describe("error codes and catalog", () => {
  it.each(["bad_mode", "confirm_required", "no_git", "bad_path", "unknown_point", "not_found", "restore_failed", "protected"])("translates %s in both languages", (code) => {
    for (const locale of ["es", "en"] as const) {
      setLocale(locale);
      const text = permissionErrorText(new ApiError(400, code, "server text"));
      expect(text).toBe(getCatalog(locale)[`permissions.err.${code}`]);
      expect(text).not.toBe("server text");
      expect(text).not.toContain("permissions.err");
    }
  });

  it("keeps the server text for a code it does not know", () => {
    expect(permissionErrorText(new ApiError(404, "unknown_project", "That project does not exist."))).toBe("That project does not exist.");
  });

  it("has a title and a reason for each of the 7 rules in es and en, and no long dash", () => {
    for (const locale of ["es", "en"] as const) {
      const catalog = getCatalog(locale);
      for (const rule of RULE_IDS) {
        expect(catalog[`block.title.${rule}`], `${locale} ${rule}`).toBeTruthy();
        expect(catalog[`block.why.${rule}`], `${locale} ${rule}`).toBeTruthy();
        expect(catalog[`protections.${rule}`], `${locale} ${rule}`).toBeTruthy();
      }
      for (const [key, text] of Object.entries(catalog)) {
        if (/^(permissions|block|trash|points|protections)\./.test(key)) expect(text, key).not.toContain(String.fromCharCode(0x2014));
      }
    }
  });
});
