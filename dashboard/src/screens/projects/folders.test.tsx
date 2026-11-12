import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { renderWithApp } from "@/test/utils";
import { ProjectsPanel } from "./ProjectsPanel";
import { resetProjectsStore } from "./store";
import type { DiskFolder } from "./foldersApi";

/** In-memory copy of the disk folders contract plus the minimum of the projects API. */
class FakeFolders {
  folders: DiskFolder[] = [
    { id: 1, project_id: 1, path: "/work/projects/p1", own: true, label: null, status: "ready", status_detail: null, file_count: 2, last_scan_at: null },
  ];
  nextId = 2;
  pick: { available: boolean; path: string | null; cancelled: boolean } = { available: true, path: null, cancelled: true };
  reject: { status: number; code: string; message: string } | null = null;
  calls: { method: string; path: string; body: unknown }[] = [];

  json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

  handle = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://x");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    this.calls.push({ method, path: url.pathname, body });
    const route = `${method} ${url.pathname.replace(/\/folders\/\d+/, "/folders/:f")}`;
    switch (route) {
      case "GET /api/projects":
        return this.json({
          workdir: "/work", root: "/work/projects", active_id: 1, kinds: [],
          projects: [{ id: 1, name: "Fondo Cedro", slug: "fondo-cedro", kind: "ips", path: "/work/projects/p1", active: true, exists: true, files: 0, git: { ok: true, reason: "" } }],
        });
      case "GET /api/projects/1/tree":
        return this.json({ id: 1, name: "Fondo Cedro", path: "/work/projects/p1", entries: [], truncated: false, git: { ok: true, reason: "" }, commits: [] });
      case "GET /api/projects/1/folders":
        return this.json({ folders: this.folders, max_folders: 5 });
      case "POST /api/system/pick-folder":
        return this.json(this.pick);
      case "POST /api/projects/1/folders": {
        if (this.reject) return this.json({ detail: { code: this.reject.code, message: this.reject.message } }, this.reject.status);
        const folder: DiskFolder = {
          id: this.nextId++, project_id: 1, path: String(body.path), own: false, label: null, status: "scanning", status_detail: null, file_count: 0, last_scan_at: null,
        };
        this.folders.push(folder);
        return this.json(folder, 201);
      }
      case "POST /api/projects/1/folders/:f/rescan": {
        const id = Number(url.pathname.split("/")[5]);
        this.folders = this.folders.map((f) => (f.id === id ? { ...f, status: "ready", file_count: 7 } : f));
        return this.json(this.folders.find((f) => f.id === id));
      }
      case "DELETE /api/projects/1/folders/:f": {
        const id = Number(url.pathname.split("/").pop());
        this.folders = this.folders.filter((f) => f.id !== id);
        return this.json({ removed: true });
      }
      default:
        return this.json({ detail: `unexpected ${route}` }, 404);
    }
  };
}

let backend: FakeFolders;

function Probe() {
  const shell = useShell();
  return (
    <ul aria-label="toasts">{shell.toasts.map((toast) => <li key={toast.id}>{toast.text}</li>)}</ul>
  );
}

async function mountFolders() {
  setToken("test-session-token-0123456789");
  renderWithApp(
    <aside aria-label="panel">
      <ProjectsPanel />
      <Probe />
    </aside>,
  );
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: t("folders.tab") }));
  return user;
}

beforeEach(() => {
  resetProjectsStore();
  backend = new FakeFolders();
  vi.stubGlobal("fetch", vi.fn(backend.handle));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe.each(["es", "en"] as const)("disk folders tab (%s)", (locale) => {
  beforeEach(() => setLocale(locale));

  it("replaces the Documents tab: only Files and Disk folders exist", async () => {
    await mountFolders();
    const tabs = screen.getByRole("group", { name: t("docs.tab.label") });
    expect(within(tabs).getAllByRole("button").map((b) => b.textContent)).toEqual([t("docs.tab.files"), t("folders.tab")]);
    expect(screen.queryByTestId("documents-section")).not.toBeInTheDocument();
  });

  it("lists the project's own folder, already registered, with status and count and no Remove", async () => {
    await mountFolders();
    const list = await screen.findByRole("list", { name: t("folders.list") });
    const row = within(list).getByRole("listitem");
    expect(row).toHaveTextContent(t("folders.own"));
    expect(row).toHaveTextContent(t("folders.status.ready"));
    expect(row).toHaveTextContent(t("folders.files", { n: 2 }));
    expect(within(row).queryByRole("button", { name: /Quitar|Remove/ })).not.toBeInTheDocument();
    expect(screen.getByText(t("folders.help"))).toBeInTheDocument();
  });

  it("adds the folder the system dialog returns", async () => {
    backend.pick = { available: true, path: "/home/ana/Papers", cancelled: false };
    const user = await mountFolders();
    await screen.findByRole("list", { name: t("folders.list") });
    await user.click(screen.getByRole("button", { name: t("folders.add") }));
    expect(await screen.findByText("/home/ana/Papers")).toBeInTheDocument();
    expect(backend.calls.find((c) => c.method === "POST" && c.path === "/api/projects/1/folders")?.body).toEqual({ path: "/home/ana/Papers" });
    expect(screen.getByRole("list", { name: "toasts" })).toHaveTextContent(t("folders.toast.added"));
  });

  it("does nothing when the student closes the dialog", async () => {
    const user = await mountFolders();
    await screen.findByRole("list", { name: t("folders.list") });
    await user.click(screen.getByRole("button", { name: t("folders.add") }));
    await waitFor(() => expect(backend.calls.some((c) => c.path === "/api/system/pick-folder")).toBe(true));
    expect(backend.calls.some((c) => c.method === "POST" && c.path === "/api/projects/1/folders")).toBe(false);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("offers a field to paste the path when the machine has no dialog, and never asks for a console", async () => {
    backend.pick = { available: false, path: null, cancelled: false };
    const user = await mountFolders();
    await screen.findByRole("list", { name: t("folders.list") });
    await user.click(screen.getByRole("button", { name: t("folders.add") }));
    await user.type(await screen.findByRole("textbox", { name: t("folders.paste.label") }), "/home/ana/Notas");
    await user.click(screen.getByRole("button", { name: t("folders.paste.confirm") }));
    expect(await screen.findByText("/home/ana/Notas")).toBeInTheDocument();
    expect(document.body.textContent ?? "").not.toMatch(/terminal|consola|console|command/i);
  });

  it("shows the backend's plain-language reason when a folder is rejected", async () => {
    backend.pick = { available: true, path: "/", cancelled: false };
    backend.reject = { status: 400, code: "disk_root", message: "No se puede agregar un disco completo." };
    const user = await mountFolders();
    await screen.findByRole("list", { name: t("folders.list") });
    await user.click(screen.getByRole("button", { name: t("folders.add") }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se puede agregar un disco completo.");
  });

  it("rescans and removes a folder of the student", async () => {
    backend.folders.push({ id: 2, project_id: 1, path: "/home/ana/Papers", own: false, label: null, status: "scanning", status_detail: null, file_count: 0, last_scan_at: null });
    const user = await mountFolders();
    await screen.findByText("/home/ana/Papers");
    await user.click(screen.getByRole("button", { name: t("folders.rescanNamed", { name: "/home/ana/Papers" }) }));
    await waitFor(() => expect(screen.getByText(t("folders.files", { n: 7 }))).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: t("folders.removeNamed", { name: "/home/ana/Papers" }) }));
    await waitFor(() => expect(screen.queryByText("/home/ana/Papers")).not.toBeInTheDocument());
    expect(screen.getByRole("list", { name: "toasts" })).toHaveTextContent(t("folders.toast.removed"));
  });

  it("disables adding at the limit and says why", async () => {
    for (let i = 0; i < 5; i += 1) {
      backend.folders.push({ id: 10 + i, project_id: 1, path: `/d/${i}`, own: false, label: null, status: "ready", status_detail: null, file_count: 1, last_scan_at: null });
    }
    await mountFolders();
    await screen.findByRole("list", { name: t("folders.list") });
    expect(screen.getByRole("button", { name: t("folders.add") })).toBeDisabled();
    expect(screen.getByText(t("folders.full", { n: 5 }))).toBeInTheDocument();
  });

  it("shows the missing status and its note", async () => {
    backend.folders.push({ id: 3, project_id: 1, path: "/gone", own: false, label: null, status: "missing", status_detail: "La carpeta no está en el disco.", file_count: 4, last_scan_at: null });
    await mountFolders();
    const row = (await screen.findByText("/gone")).closest("li")!;
    expect(row).toHaveTextContent(t("folders.status.missing"));
    expect(row).toHaveTextContent("La carpeta no está en el disco.");
  });
});
