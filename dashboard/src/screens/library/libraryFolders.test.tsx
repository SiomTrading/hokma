import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { getCatalog, setLocale, t } from "@/i18n";
import type { DocumentInfo } from "@/screens/projects/documentsTypes";
import { useShell } from "@/shell/useShell";
import { renderWithApp } from "@/test/utils";
import { LibraryScreen } from "./LibraryScreen";

interface Folder {
  id: number;
  parent_id: number | null;
  name: string;
}

/** In-memory copy of the folder part of the Library contract (SPEC-010 4.8). */
class FakeFolders {
  folders: Folder[] = [];
  docs: (DocumentInfo & { library_folder_id: number | null })[] = [];
  nextId = 1;
  calls: { method: string; path: string; search: string; body: unknown }[] = [];

  json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

  private pathOf(id: number | null): string {
    const parts: string[] = [];
    let cursor = id;
    while (cursor != null) {
      const folder = this.folders.find((f) => f.id === cursor)!;
      parts.unshift(folder.name);
      cursor = folder.parent_id;
    }
    return parts.join("/");
  }

  private descendants(id: number): number[] {
    const kids = this.folders.filter((f) => f.parent_id === id).map((f) => f.id);
    return [id, ...kids.flatMap((k) => this.descendants(k))];
  }

  addFolder(name: string, parentId: number | null = null): Folder {
    const folder = { id: this.nextId++, parent_id: parentId, name };
    this.folders.push(folder);
    return folder;
  }

  addDoc(name: string, folderId: number | null = null): DocumentInfo {
    const doc = {
      id: this.nextId++, name, format: "pdf", size_bytes: 5, status: "ready" as const, status_detail: null, chunk_count: 2,
      source_url: null, description: null, origin_project_id: null, library_path: this.pathOf(folderId), library_folder_id: folderId,
    };
    this.docs.push(doc);
    return doc;
  }

  handle = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://x");
    const method = init?.method ?? "GET";
    const body = init?.body;
    this.calls.push({ method, path: url.pathname, search: url.search, body });
    const route = `${method} ${url.pathname.replace(/\/(folders|documents)\/\d+$/, "/$1/:id")}`;
    const id = Number(url.pathname.split("/").pop());
    switch (route) {
      case "GET /api/projects":
        return this.json({ workdir: "/w", root: "/w/p", active_id: null, kinds: [], projects: [] });
      case "GET /api/library/model":
        return this.json({ state: "ready", percent: 100, bytes: 1, total: 1 });
      case "GET /api/library/folders": {
        const parent = url.searchParams.get("parent_id");
        const parentId = parent == null ? null : Number(parent);
        return this.json({ folders: this.folders.filter((f) => f.parent_id === parentId) });
      }
      case "POST /api/library/folders": {
        const sent = JSON.parse(String(body)) as { parent_id: number | null; name: string };
        if (this.folders.some((f) => f.parent_id === sent.parent_id && f.name.toLowerCase() === sent.name.toLowerCase())) {
          return this.json({ detail: { code: "duplicate_name", message: "dup" } }, 409);
        }
        return this.json(this.addFolder(sent.name, sent.parent_id), 201);
      }
      case "PATCH /api/library/folders/:id": {
        const sent = JSON.parse(String(body)) as { name: string };
        const folder = this.folders.find((f) => f.id === id)!;
        if (this.folders.some((f) => f.id !== id && f.parent_id === folder.parent_id && f.name.toLowerCase() === sent.name.toLowerCase())) {
          return this.json({ detail: { code: "duplicate_name", message: "dup" } }, 409);
        }
        folder.name = sent.name;
        return this.json(folder);
      }
      case "DELETE /api/library/folders/:id": {
        const ids = this.descendants(id);
        const documents = this.docs.filter((d) => d.library_folder_id != null && ids.includes(d.library_folder_id)).length;
        const subfolders = ids.length - 1;
        if ((documents > 0 || subfolders > 0) && url.searchParams.get("confirm") !== "true") {
          return this.json({ detail: { code: "folder_not_empty", message: "full", documents, subfolders } }, 409);
        }
        this.folders = this.folders.filter((f) => !ids.includes(f.id));
        this.docs = this.docs.filter((d) => d.library_folder_id == null || !ids.includes(d.library_folder_id));
        return new Response(null, { status: 204 });
      }
      case "GET /api/library/documents": {
        const folder = url.searchParams.get("folder_id");
        const list = folder == null ? this.docs : this.docs.filter((d) => d.library_folder_id === Number(folder));
        return this.json({ documents: list });
      }
      case "POST /api/library/documents": {
        const form = body as FormData;
        const file = form.get("file") as File;
        const folder = url.searchParams.get("folder_id");
        return this.json(this.addDoc(file.name, folder == null ? null : Number(folder)), 201);
      }
      case "PATCH /api/library/documents/:id": {
        const sent = JSON.parse(String(body)) as { library_folder_id: number | null };
        const doc = this.docs.find((d) => d.id === id)!;
        if (sent.library_folder_id != null && !this.folders.some((f) => f.id === sent.library_folder_id)) return this.json({ detail: "gone" }, 404);
        if (this.docs.some((d) => d.id !== id && d.name === doc.name && d.library_folder_id === sent.library_folder_id)) {
          return this.json({ detail: { code: "duplicate_name", message: "dup" } }, 409);
        }
        doc.library_folder_id = sent.library_folder_id;
        doc.library_path = this.pathOf(sent.library_folder_id);
        return this.json(doc);
      }
      default:
        return this.json({ detail: `unexpected ${route}` }, 404);
    }
  };
}

let backend: FakeFolders;

function ToastProbe() {
  const shell = useShell();
  return (
    <ul aria-label="toasts">
      {shell.toasts.map((toast) => (
        <li key={toast.id}>{toast.text}</li>
      ))}
    </ul>
  );
}

async function mount() {
  setToken("test-session-token-0123456789");
  renderWithApp(
    <>
      <LibraryScreen />
      <ToastProbe />
    </>,
  );
  return userEvent.setup();
}

const crumbs = () => screen.getByRole("navigation", { name: t("library.crumbs.label") });
const folderList = () => screen.findByRole("list", { name: t("library.folders.list") });
const dragData = (id: number) => {
  const store = new Map<string, string>([["application/x-hokma-document", String(id)]]);
  return { types: ["application/x-hokma-document"], getData: (type: string) => store.get(type) ?? "", setData: (type: string, value: string) => void store.set(type, value), effectAllowed: "" };
};

beforeEach(() => {
  backend = new FakeFolders();
  vi.stubGlobal("fetch", vi.fn(backend.handle));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe.each(["es", "en"] as const)("Library folders (%s)", (locale) => {
  beforeEach(() => setLocale(locale));

  it("creates three levels, enters each and goes back through the path", async () => {
    const user = await mount();
    await screen.findByText(t("library.empty.title"));
    for (const name of ["Papers", "2026", "Q1"]) {
      await user.click(screen.getByRole("button", { name: t("library.folder.new") }));
      await user.type(screen.getByRole("textbox", { name: t("library.folder.name") }), name);
      await user.click(screen.getByRole("button", { name: t("library.folder.create") }));
      await user.click(await within(await folderList()).findByRole("button", { name }));
    }
    const path = crumbs();
    expect(within(path).getAllByRole("button").map((b) => b.textContent)).toEqual([t("library.title"), "Papers", "2026", "Q1"]);
    expect(backend.folders.map((f) => [f.name, f.parent_id])).toEqual([["Papers", null], ["2026", 1], ["Q1", 2]]);
    // Back two levels with one click on a path segment.
    await user.click(within(path).getByRole("button", { name: "Papers" }));
    expect(within(crumbs()).getAllByRole("button").map((b) => b.textContent)).toEqual([t("library.title"), "Papers"]);
    expect(await within(await folderList()).findByRole("button", { name: "2026" })).toBeInTheDocument();
    await user.click(within(crumbs()).getByRole("button", { name: t("library.title") }));
    expect(await within(await folderList()).findByRole("button", { name: "Papers" })).toBeInTheDocument();
  });

  it("shows only the documents of the level in view and keeps the root list to the root", async () => {
    const papers = backend.addFolder("Papers");
    backend.addDoc("root.pdf");
    backend.addDoc("inside.pdf", papers.id);
    const user = await mount();
    await screen.findByText("root.pdf");
    expect(screen.queryByText("inside.pdf")).not.toBeInTheDocument();
    await user.click(within(await folderList()).getByRole("button", { name: "Papers" }));
    await screen.findByText("inside.pdf");
    expect(screen.queryByText("root.pdf")).not.toBeInTheDocument();
    expect(backend.calls.some((c) => c.path === "/api/library/documents" && c.search === `?folder_id=${papers.id}`)).toBe(true);
  });

  it("uploads into the folder in view", async () => {
    const papers = backend.addFolder("Papers");
    const user = await mount();
    await user.click(within(await folderList()).getByRole("button", { name: "Papers" }));
    await screen.findByText(t("library.empty.title"));
    await user.upload(screen.getByTestId("documents-file-input"), new File(["%PDF"], "new.pdf", { type: "application/pdf" }));
    await screen.findByText("new.pdf");
    const upload = backend.calls.find((c) => c.method === "POST" && c.path === "/api/library/documents")!;
    expect(new URLSearchParams(upload.search).get("folder_id")).toBe(String(papers.id));
    expect(backend.docs[0]!.library_folder_id).toBe(papers.id);
  });

  it("moves a document by dropping it on a folder and on a path segment", async () => {
    const papers = backend.addFolder("Papers");
    const doc = backend.addDoc("move-me.pdf");
    const user = await mount();
    await screen.findByText("move-me.pdf");
    const target = within(await folderList()).getByRole("button", { name: "Papers" }).closest("li")!;
    fireEvent.drop(target, { dataTransfer: dragData(doc.id) });
    await waitFor(() => expect(screen.queryByText("move-me.pdf")).not.toBeInTheDocument());
    expect(backend.docs[0]!.library_folder_id).toBe(papers.id);
    expect(screen.getByRole("list", { name: "toasts" })).toHaveTextContent(t("library.move.toast", { folder: "Papers" }));
    // Inside the folder, drop it on the first segment of the path: it goes back to the root.
    await user.click(within(await folderList()).getByRole("button", { name: "Papers" }));
    await screen.findByText("move-me.pdf");
    fireEvent.drop(within(crumbs()).getByRole("button", { name: t("library.title") }), { dataTransfer: dragData(doc.id) });
    await waitFor(() => expect(screen.queryByText("move-me.pdf")).not.toBeInTheDocument());
    expect(backend.docs[0]!.library_folder_id).toBeNull();
  });

  it("makes document rows draggable and says so in plain words when the move is refused", async () => {
    const papers = backend.addFolder("Papers");
    const doc = backend.addDoc("same.pdf");
    backend.addDoc("same.pdf", papers.id);
    await mount();
    const item = (await screen.findByText("same.pdf")).closest("li")!;
    expect(item).toHaveAttribute("draggable", "true");
    const target = within(await folderList()).getByRole("button", { name: "Papers" }).closest("li")!;
    fireEvent.drop(target, { dataTransfer: dragData(doc.id) });
    await waitFor(() => expect(screen.getByRole("list", { name: "toasts" })).toHaveTextContent(t("library.move.error.duplicate")));
    expect(screen.getByText("same.pdf")).toBeInTheDocument();
  });

  it("ignores drops that carry no Library document", async () => {
    backend.addFolder("Papers");
    await mount();
    const target = within(await folderList()).getByRole("button", { name: "Papers" }).closest("li")!;
    fireEvent.drop(target, { dataTransfer: { types: ["Files"], files: [], getData: () => "" } });
    expect(backend.calls.some((c) => c.method === "PATCH")).toBe(false);
  });

  it("renames from the menu and by double click, and refuses a repeated name", async () => {
    backend.addFolder("Papers");
    backend.addFolder("Notes");
    const user = await mount();
    const list = await folderList();
    await user.click(within(list).getByRole("button", { name: t("library.folder.menu", { name: "Papers" }) }));
    await user.click(screen.getByRole("menuitem", { name: t("library.folder.rename") }));
    const field = screen.getByRole("textbox", { name: t("library.folder.renameField", { name: "Papers" }) });
    await user.clear(field);
    await user.type(field, "notes");
    await user.click(screen.getByRole("button", { name: t("library.folder.save") }));
    expect(await screen.findByRole("alert")).toHaveTextContent(t("library.folder.error.duplicate"));
    await user.clear(field);
    await user.type(field, "Articles");
    await user.click(screen.getByRole("button", { name: t("library.folder.save") }));
    expect(await within(await folderList()).findByRole("button", { name: "Articles" })).toBeInTheDocument();
    fireEvent.doubleClick(within(await folderList()).getByRole("button", { name: "Notes" }));
    expect(screen.getByRole("textbox", { name: t("library.folder.renameField", { name: "Notes" }) })).toBeInTheDocument();
  });

  it("removes an empty folder without asking", async () => {
    backend.addFolder("Empty");
    const user = await mount();
    await user.click(await screen.findByRole("button", { name: t("library.folder.menu", { name: "Empty" }) }));
    await user.click(screen.getByRole("menuitem", { name: t("library.folder.remove") }));
    await waitFor(() => expect(screen.queryByRole("list", { name: t("library.folders.list") })).not.toBeInTheDocument());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(backend.folders).toHaveLength(0);
  });

  it("asks to confirm a folder with contents, with the counts, and removes it only after confirming", async () => {
    const papers = backend.addFolder("Papers");
    backend.addFolder("2026", papers.id);
    backend.addDoc("a.pdf", papers.id);
    backend.addDoc("b.pdf", papers.id);
    const user = await mount();
    await user.click(await screen.findByRole("button", { name: t("library.folder.menu", { name: "Papers" }) }));
    await user.click(screen.getByRole("menuitem", { name: t("library.folder.remove") }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(
      t("library.folder.confirm.body", { documents: t("library.folder.count.documents", { n: 2 }), subfolders: t("library.folder.count.subfolder", { n: 1 }) }),
    );
    expect(backend.folders).toHaveLength(2);
    // Keeping it removes nothing.
    await user.click(within(dialog).getByRole("button", { name: t("library.folder.keep") }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(backend.folders).toHaveLength(2);
    // Confirming removes the folder with everything inside.
    await user.click(screen.getByRole("button", { name: t("library.folder.menu", { name: "Papers" }) }));
    await user.click(screen.getByRole("menuitem", { name: t("library.folder.remove") }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: t("library.folder.remove") }));
    await waitFor(() => expect(backend.folders).toHaveLength(0));
    expect(backend.docs).toHaveLength(0);
    expect(backend.calls.some((c) => c.method === "DELETE" && c.search === "?confirm=true")).toBe(true);
  });

  it("asks for a name before creating a folder and says so when the name is taken", async () => {
    backend.addFolder("Papers");
    const user = await mount();
    await screen.findByRole("list", { name: t("library.folders.list") });
    await user.click(screen.getByRole("button", { name: t("library.folder.new") }));
    await user.click(screen.getByRole("button", { name: t("library.folder.create") }));
    expect(await screen.findByRole("alert")).toHaveTextContent(t("library.folder.error.name"));
    await user.type(screen.getByRole("textbox", { name: t("library.folder.name") }), "papers");
    await user.click(screen.getByRole("button", { name: t("library.folder.create") }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(t("library.folder.error.duplicate")));
    expect(backend.folders).toHaveLength(1);
  });
});

describe("folder texts", () => {
  it("every folder text exists in es and en with the same placeholders", () => {
    const es = getCatalog("es");
    const en = getCatalog("en");
    const keys = Object.keys(es).filter((k) => k.startsWith("library.folder") || k.startsWith("library.move") || k.startsWith("library.crumbs"));
    expect(keys.length).toBeGreaterThan(20);
    for (const key of keys) {
      expect(en[key], key).toBeTruthy();
      const holes = (text: string) => (text.match(/\{\w+\}/g) ?? []).sort().join(",");
      expect(holes(en[key]!), key).toBe(holes(es[key]!));
    }
  });

  it("the visible words of the folder controls are Spanish in es", () => {
    setLocale("es");
    expect(t("library.folder.new")).toBe("Nueva carpeta");
    expect(t("library.folder.rename")).toBe("Renombrar");
    expect(t("library.folder.remove")).toBe("Quitar");
    expect(t("library.folder.confirm.body", { documents: "3 documentos", subfolders: "2 subcarpetas" })).toBe(
      "Se quitarán 3 documentos y 2 subcarpetas de la Biblioteca. No se puede deshacer.",
    );
  });
});

