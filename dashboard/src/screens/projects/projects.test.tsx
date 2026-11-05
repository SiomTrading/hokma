import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { useShellEvent } from "@/shell/store";
import { renderWithApp } from "@/test/utils";
import { CreateProjectButton, ProjectsPanel } from "./ProjectsPanel";
import { resetProjectsStore } from "./store";
import type { ProjectInfo, ProjectKind, TreeEntry } from "./types";

interface FakeProject extends ProjectInfo {
  entries: TreeEntry[];
  texts: Record<string, string>;
}

function readText(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(file);
  });
}

/** An in-memory copy of the projects API: it keeps state, so a flow can be followed from one call to the next. */
class FakeBackend {
  projects: FakeProject[] = [];
  activeId: number | null = null;
  calls: { method: string; path: string; body: unknown }[] = [];
  nextId = 1;

  constructor(seed = true) {
    if (seed) this.add("Fondo Cedro", "ips");
  }

  add(name: string, kind: ProjectKind): FakeProject {
    const id = this.nextId++;
    const project: FakeProject = {
      id, name, slug: name.toLowerCase().replace(/\W+/g, "-"), kind, path: `/work/projects/p${id}`, active: false, exists: true,
      files: 0, git: { ok: true, reason: "" },
      entries: ["contexto", "documentos", "imagenes", "audios", "codigo", "resultados"].map((n) => ({ path: n, name: n, type: "dir" as const, depth: 0 })),
      texts: {},
    };
    this.projects.push(project);
    this.activeId ??= id;
    return project;
  }

  list() {
    return {
      workdir: "/work", root: "/work/projects", active_id: this.activeId, kinds: [],
      projects: this.projects.map((p) => ({ ...p, active: p.id === this.activeId, entries: undefined, texts: undefined })),
    };
  }

  json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

  handle = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://x");
    const method = init?.method ?? "GET";
    let body: unknown = init?.body;
    if (typeof body === "string") body = JSON.parse(body);
    this.calls.push({ method, path: url.pathname, body });
    const m = url.pathname.match(/^\/api\/projects\/(\d+)(\/\w+)?$/);
    const project = m ? this.projects.find((p) => p.id === Number(m[1])) : undefined;
    const route = `${method} ${m ? `/api/projects/:id${m[2] ?? ""}` : url.pathname}`;
    switch (route) {
      case "GET /api/projects":
        return this.json(this.list());
      case "POST /api/projects": {
        const b = body as { name: string; kind: ProjectKind };
        if (b.name === "Repetido") return this.json({ detail: { code: "db_failed", message: "No se pudo guardar el proyecto." } }, 500);
        return this.json(this.add(b.name, b.kind), 201);
      }
      case "PATCH /api/projects/:id":
        project!.name = (body as { name: string }).name;
        return this.json(project);
      case "POST /api/projects/:id/activate":
        this.activeId = project!.id;
        return this.json(this.list());
      case "GET /api/projects/:id/tree":
        return this.json({ id: project!.id, name: project!.name, path: project!.path, entries: project!.entries, truncated: false, git: project!.git, commits: [] });
      case "POST /api/projects/:id/files": {
        const file = (body as FormData).get("file") as File;
        const folder = file.type.startsWith("image/") ? "imagenes" : "documentos";
        const path = `${folder}/${file.name}`;
        project!.entries.push({ path, name: file.name, type: "file", depth: 1, size: file.size });
        project!.texts[path] = await readText(file);
        project!.files += 1;
        return this.json({ path, size: file.size, created: true, git: project!.git }, 201);
      }
      case "POST /api/projects/:id/links": {
        const b = body as { url: string; download: boolean };
        if (b.url.includes("10.0.0.1")) return this.json({ detail: { code: "url_private", message: "That link points to a private or internal address and was refused." } }, 400);
        const file = b.download && b.url.endsWith(".pdf") ? "documentos/informe.pdf" : null;
        if (file) project!.entries.push({ path: file, name: "informe.pdf", type: "file", depth: 1, size: 2048 });
        return this.json({ link: b.url, file, downloaded: file !== null, git: project!.git }, 201);
      }
      case "GET /api/projects/:id/file": {
        const path = url.searchParams.get("path")!;
        const text = project!.texts[path] ?? "";
        const limit = Number(url.searchParams.get("limit") ?? 0);
        return new Response(limit ? text.slice(0, limit) : text, { headers: { "content-type": "text/plain; charset=utf-8" } });
      }
      default:
        return this.json({ detail: `unexpected ${route}` }, 404);
    }
  };

  called(method: string, path: string) {
    return this.calls.filter((c) => c.method === method && c.path === path);
  }
}

let backend: FakeBackend;

const announced: unknown[] = [];

function ToastProbe() {
  const shell = useShell();
  useShellEvent("project:active", (payload) => announced.push(payload));
  return (
    <ul aria-label="toasts">
      {shell.toasts.map((toast) => (
        <li key={toast.id}>{toast.text}</li>
      ))}
    </ul>
  );
}

function mount() {
  setToken("test-session-token-0123456789");
  return renderWithApp(
    <aside aria-label={t("rightPanel.title")}>
      <CreateProjectButton />
      <ProjectsPanel />
      <ToastProbe />
    </aside>,
  );
}

function panel() {
  return screen.getByRole("complementary", { name: t("rightPanel.title") });
}

beforeEach(() => {
  resetProjectsStore();
  announced.length = 0;
  backend = new FakeBackend();
  vi.stubGlobal("fetch", vi.fn(backend.handle));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe.each(["es", "en"] as const)("projects panel (%s)", (locale) => {
  beforeEach(() => setLocale(locale));

  it("lists the projects with their file count and Git, and the folders of the active one", async () => {
    backend.add("Robot NQ", "robot").files = 3;
    mount();
    const list = await within(panel()).findByRole("list", { name: t("projects.list") });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("Fondo Cedro");
    expect(within(items[0]!).getByRole("button")).toHaveAttribute("aria-current", "true");
    expect(items[1]).toHaveTextContent(t("projects.filesGit", { n: 3 }));
    expect(within(panel()).getByText("/work")).toBeInTheDocument();
    expect(await within(panel()).findByRole("treeitem", { name: t("projects.collapse", { name: "contexto" }) })).toBeInTheDocument();
    expect(within(panel()).getByText(t("projects.drop.title"))).toBeInTheDocument();
    expect(within(panel()).getByText(t("projects.drop.sub"))).toBeInTheDocument();
  });

  it("changes the active project, reloads its tree and announces it", async () => {
    const other = backend.add("Robot NQ", "robot");
    other.entries.push({ path: "codigo/bot.py", name: "bot.py", type: "file", depth: 1, size: 900 });
    mount();
    const user = userEvent.setup();
    await within(panel()).findByRole("list");
    await user.click(await within(panel()).findByRole("button", { name: /Robot NQ/ }));
    expect(await within(panel()).findByRole("treeitem", { name: t("projects.open", { name: "bot.py" }) })).toBeInTheDocument();
    expect(backend.called("POST", `/api/projects/${other.id}/activate`)).toHaveLength(1);
    expect(within(panel()).getByRole("button", { name: /Robot NQ/ })).toHaveAttribute("aria-current", "true");
    expect(announced).toEqual([{ id: "1", name: "Fondo Cedro" }, { id: String(other.id), name: "Robot NQ" }]);
  });

  it("creates a project from the + button and makes it the active one", async () => {
    mount();
    const user = userEvent.setup();
    await within(panel()).findByRole("list");
    await user.click(screen.getByRole("button", { name: t("projects.create") }));
    const dialog = await screen.findByRole("dialog", { name: t("projects.dialog.createTitle") });
    await user.type(within(dialog).getByLabelText(t("projects.field.name")), "Cobertura SPX");
    await user.selectOptions(within(dialog).getByLabelText(t("projects.field.kind")), "strategy");
    await user.click(within(dialog).getByRole("button", { name: t("projects.create") }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const item = await within(panel()).findByRole("button", { name: /Cobertura SPX/ });
    expect(item).toHaveAttribute("aria-current", "true");
    expect(backend.called("POST", "/api/projects")[0]!.body).toEqual({ name: "Cobertura SPX", kind: "strategy" });
  });

  it("asks for a name and shows the server error without closing the dialog", async () => {
    mount();
    const user = userEvent.setup();
    await within(panel()).findByRole("list");
    await user.click(screen.getByRole("button", { name: t("projects.create") }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: t("projects.create") }));
    expect(await within(dialog).findByText(t("projects.error.name"))).toBeInTheDocument();
    expect(backend.called("POST", "/api/projects")).toHaveLength(0);
    await user.type(within(dialog).getByLabelText(t("projects.field.name")), "Repetido");
    await user.click(within(dialog).getByRole("button", { name: t("projects.create") }));
    expect(await within(dialog).findByText("No se pudo guardar el proyecto.")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("renames the active project", async () => {
    mount();
    const user = userEvent.setup();
    await within(panel()).findByRole("list");
    await user.click(await within(panel()).findByRole("button", { name: t("projects.rename") }));
    const dialog = await screen.findByRole("dialog", { name: t("projects.dialog.renameTitle") });
    const input = within(dialog).getByLabelText(t("projects.field.name"));
    await user.clear(input);
    await user.type(input, "Fondo Nuevo");
    await user.click(within(dialog).getByRole("button", { name: t("projects.save") }));
    expect(await within(panel()).findByRole("button", { name: /Fondo Nuevo/ })).toBeInTheDocument();
    expect(backend.called("PATCH", "/api/projects/1")[0]!.body).toEqual({ name: "Fondo Nuevo" });
  });

  it("uploads dropped files and shows them in the tree", async () => {
    mount();
    await within(panel()).findByRole("list");
    const tree = await within(panel()).findByTestId("project-tree");
    const file = new File(["precio 100"], "notas.txt", { type: "text/plain" });
    fireEvent.drop(tree, { dataTransfer: { files: [file], types: ["Files"] } });
    expect(await within(panel()).findByRole("treeitem", { name: t("projects.open", { name: "notas.txt" }) })).toBeInTheDocument();
    expect(backend.called("POST", "/api/projects/1/files")).toHaveLength(1);
    expect(await screen.findByText(t("projects.toast.uploaded", { path: "documentos/notas.txt" }))).toBeInTheDocument();
  });

  it("uploads files chosen with the button and refuses the ones above 25 MB without sending them", async () => {
    mount();
    await within(panel()).findByRole("list");
    const input = await within(panel()).findByTestId("project-file-input");
    const big = new File(["x"], "enorme.bin");
    Object.defineProperty(big, "size", { value: 26 * 1024 * 1024 });
    fireEvent.change(input, { target: { files: [big, new File(["a"], "ok.txt", { type: "text/plain" })] } });
    expect(await screen.findByText(t("projects.toast.tooBig", { name: "enorme.bin" }))).toBeInTheDocument();
    await waitFor(() => expect(backend.called("POST", "/api/projects/1/files")).toHaveLength(1));
  });

  it("previews a text file", async () => {
    mount();
    const user = userEvent.setup();
    await within(panel()).findByRole("list");
    const file = new File(["<b>no es html</b>"], "n.txt", { type: "text/plain" });
    fireEvent.drop(await within(panel()).findByTestId("project-tree"), { dataTransfer: { files: [file], types: ["Files"] } });
    await user.click(await within(panel()).findByRole("treeitem", { name: t("projects.open", { name: "n.txt" }) }));
    const dialog = await screen.findByRole("dialog", { name: t("projects.preview.title", { name: "n.txt" }) });
    const text = await within(dialog).findByText("<b>no es html</b>");
    expect(text.tagName).toBe("PRE");
    expect(dialog.querySelector("b")).toBeNull();
  });

  it("adds a link, downloads the file it points to, and shows the server refusal for a private address", async () => {
    mount();
    const user = userEvent.setup();
    await within(panel()).findByRole("list");
    await user.click(await within(panel()).findByRole("button", { name: t("projects.addLink") }));
    let dialog = await screen.findByRole("dialog", { name: t("projects.dialog.linkTitle") });
    await user.type(within(dialog).getByLabelText(t("projects.field.url")), "http://x.com/a.pdf");
    await user.click(within(dialog).getByRole("button", { name: t("projects.addLink") }));
    expect(await within(dialog).findByText(t("projects.error.url"))).toBeInTheDocument();
    await user.clear(within(dialog).getByLabelText(t("projects.field.url")));
    await user.type(within(dialog).getByLabelText(t("projects.field.url")), "https://10.0.0.1/a.pdf");
    await user.click(within(dialog).getByRole("button", { name: t("projects.addLink") }));
    expect(await within(dialog).findByText(/private|privada/)).toBeInTheDocument();

    await user.clear(within(dialog).getByLabelText(t("projects.field.url")));
    await user.type(within(dialog).getByLabelText(t("projects.field.url")), "https://files.example.com/informe.pdf");
    await user.type(within(dialog).getByLabelText(t("projects.field.title")), "Informe");
    await user.click(within(dialog).getByRole("button", { name: t("projects.addLink") }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await within(panel()).findByRole("treeitem", { name: t("projects.open", { name: "informe.pdf" }) })).toBeInTheDocument();
    const sent = backend.called("POST", "/api/projects/1/links").at(-1)!.body;
    expect(sent).toEqual({ url: "https://files.example.com/informe.pdf", title: "Informe", download: true });

    await user.click(await within(panel()).findByRole("button", { name: t("projects.addLink") }));
    dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("switch"));
    await user.type(within(dialog).getByLabelText(t("projects.field.url")), "https://files.example.com/otro.pdf");
    await user.click(within(dialog).getByRole("button", { name: t("projects.addLink") }));
    await waitFor(() => expect(backend.called("POST", "/api/projects/1/links")).toHaveLength(3));
    expect((backend.called("POST", "/api/projects/1/links").at(-1)!.body as { download: boolean }).download).toBe(false);
  });

  it("collapses and expands a folder", async () => {
    backend.projects[0]!.entries.push({ path: "documentos/a.md", name: "a.md", type: "file", depth: 1, size: 10 });
    mount();
    const user = userEvent.setup();
    await within(panel()).findByRole("list");
    expect(await within(panel()).findByRole("treeitem", { name: t("projects.open", { name: "a.md" }) })).toBeInTheDocument();
    await user.click(within(panel()).getByRole("treeitem", { name: t("projects.collapse", { name: "documentos" }) }));
    expect(within(panel()).queryByRole("treeitem", { name: t("projects.open", { name: "a.md" }) })).not.toBeInTheDocument();
    await user.click(within(panel()).getByRole("treeitem", { name: t("projects.expand", { name: "documentos" }) }));
    expect(within(panel()).getByRole("treeitem", { name: t("projects.open", { name: "a.md" }) })).toBeInTheDocument();
  });

  it("shows the empty state and the load error with a retry", async () => {
    backend.projects = [];
    backend.activeId = null;
    mount();
    expect(await within(panel()).findByText(t("projects.empty.title"))).toBeInTheDocument();
    resetProjectsStore();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("down")));
    cleanup();
    mount();
    expect(await within(panel()).findAllByRole("alert")).not.toHaveLength(0);
  });

  it("warns when Git is off for the project", async () => {
    backend.projects[0]!.git = { ok: false, reason: "unsafe" };
    mount();
    expect(await within(panel()).findByText(t("projects.gitOff"))).toBeInTheDocument();
    expect(within(panel()).getByRole("listitem")).toHaveTextContent(t("projects.filesNoGit", { n: 0 }));
  });
});
