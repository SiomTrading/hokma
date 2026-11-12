import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { renderWithApp } from "@/test/utils";
import { DocumentsSection } from "./DocumentsSection";
import { resetProjectsStore } from "./store";
import type { DocumentInfo } from "./documentsTypes";

/** In-memory copy of the documents contract (list, upload, delete) plus the minimum of the projects API. */
class FakeDocs {
  docs: DocumentInfo[] = [];
  nextId = 1;
  calls: { method: string; path: string; body: unknown }[] = [];
  /** Filename to HTTP status, to simulate 409, 413 and 415. */
  reject: Record<string, number> = {};
  wrapped = false;

  json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

  handle = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://x");
    const method = init?.method ?? "GET";
    const body = init?.body;
    this.calls.push({ method, path: url.pathname, body });
    const route = `${method} ${url.pathname.replace(/\/documents\/\d+$/, "/documents/:doc")}`;
    switch (route) {
      case "GET /api/projects":
        return this.json({
          workdir: "/work", root: "/work/projects", active_id: 1, kinds: [],
          projects: [{ id: 1, name: "Fondo Cedro", slug: "fondo-cedro", kind: "ips", path: "/work/projects/p1", active: true, exists: true, files: 0, git: { ok: true, reason: "" } }],
        });
      case "GET /api/projects/1/tree":
        return this.json({ id: 1, name: "Fondo Cedro", path: "/work/projects/p1", entries: [], truncated: false, git: { ok: true, reason: "" }, commits: [] });
      case "GET /api/projects/1/documents":
        return this.json(this.wrapped ? { documents: this.docs } : this.docs);
      case "POST /api/projects/1/documents": {
        const form = body as FormData;
        const file = form.get("file") as File;
        const status = this.reject[file.name];
        if (status) return this.json({ detail: { code: "rejected", message: "rejected" } }, status);
        const doc: DocumentInfo = {
          id: this.nextId++, name: file.name, format: file.name.split(".").pop()!, size_bytes: file.size, status: "ready",
          status_detail: null, chunk_count: 3, source_url: null, description: (form.get("description") as string | null) ?? null,
        };
        this.docs.push(doc);
        return this.json(doc, 201);
      }
      case "DELETE /api/projects/1/documents/:doc": {
        const id = Number(url.pathname.split("/").pop());
        this.docs = this.docs.filter((d) => d.id !== id);
        return new Response(null, { status: 204 });
      }
      default:
        return this.json({ detail: `unexpected ${route}` }, 404);
    }
  };

  uploads() {
    return this.calls.filter((c) => c.method === "POST" && c.path === "/api/projects/1/documents");
  }
}

let backend: FakeDocs;

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

async function mountDocuments() {
  setToken("test-session-token-0123456789");
  renderWithApp(
    <aside aria-label="panel">
      <DocumentsSection projectId={1} />
      <ToastProbe />
    </aside>,
  );
  return userEvent.setup();
}

const pdf = (name = "paper.pdf") => new File(["%PDF-1.4"], name, { type: "application/pdf" });

beforeEach(() => {
  resetProjectsStore();
  backend = new FakeDocs();
  vi.stubGlobal("fetch", vi.fn(backend.handle));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe.each(["es", "en"] as const)("documents tab (%s)", (locale) => {
  beforeEach(() => setLocale(locale));

  it("shows the help, the formats line and the empty state", async () => {
    await mountDocuments();
    expect(await screen.findByText(t("docs.help"))).toBeInTheDocument();
    expect(screen.getByText(t("docs.formats"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("docs.add") })).toBeInTheDocument();
    expect(await screen.findByText(t("docs.empty.title"))).toBeInTheDocument();
  });

  it("uploads a picked file and lists it with status, passages and origin", async () => {
    const user = await mountDocuments();
    await screen.findByText(t("docs.empty.title"));
    await user.upload(screen.getByTestId("documents-file-input"), pdf());
    const list = await screen.findByRole("list", { name: t("docs.list") });
    const row = within(list).getByRole("listitem");
    expect(row).toHaveTextContent("paper.pdf");
    expect(row).toHaveTextContent(t("docs.status.ready"));
    expect(row).toHaveTextContent(t("docs.passages", { n: 3 }));
    expect(row).toHaveTextContent(t("docs.origin.file"));
    expect(screen.getByRole("list", { name: "toasts" })).toHaveTextContent(t("docs.toast.added", { name: "paper.pdf" }));
  });

  it("accepts the list wrapped in a documents object", async () => {
    backend.wrapped = true;
    backend.docs.push({ id: 9, name: "tabla.csv", format: "csv", size_bytes: 10, status: "ready", status_detail: null, chunk_count: 1, source_url: null, description: null });
    await mountDocuments();
    const row = within(await screen.findByRole("list", { name: t("docs.list") })).getByRole("listitem");
    expect(row).toHaveTextContent(t("docs.passage"));
  });

  it("sends dropped files one by one", async () => {
    await mountDocuments();
    await screen.findByText(t("docs.empty.title"));
    const zone = screen.getByRole("group", { name: t("docs.drop.area") });
    fireEvent.drop(zone, { dataTransfer: { files: [pdf("a.pdf"), pdf("b.pdf")], types: ["Files"] } });
    await waitFor(() => expect(within(screen.getByRole("list", { name: t("docs.list") })).getAllByRole("listitem")).toHaveLength(2));
    expect(backend.uploads()).toHaveLength(2);
  });

  it("asks for an optional description of each image before sending it", async () => {
    const user = await mountDocuments();
    await screen.findByText(t("docs.empty.title"));
    const image = new File(["x"], "grafico.png", { type: "image/png" });
    await user.upload(screen.getByTestId("documents-file-input"), image);
    expect(backend.uploads()).toHaveLength(0);
    await user.type(screen.getByRole("textbox", { name: t("docs.image.descriptionFor", { name: "grafico.png" }) }), "equity curve");
    await user.click(screen.getByRole("button", { name: t("docs.image.confirm") }));
    const row = within(await screen.findByRole("list", { name: t("docs.list") })).getByRole("listitem");
    expect(row).toHaveTextContent("equity curve");
    const form = backend.uploads()[0]!.body as FormData;
    expect(form.get("description")).toBe("equity curve");
  });

  it("cancels pending images without sending anything", async () => {
    const user = await mountDocuments();
    await screen.findByText(t("docs.empty.title"));
    await user.upload(screen.getByTestId("documents-file-input"), new File(["x"], "a.jpg", { type: "image/jpeg" }));
    await user.click(screen.getByRole("button", { name: t("docs.image.cancel") }));
    expect(screen.queryByRole("textbox", { name: t("docs.image.descriptionFor", { name: "a.jpg" }) })).not.toBeInTheDocument();
    expect(backend.uploads()).toHaveLength(0);
  });

  it.each([
    [409, "docs.error.duplicate"],
    [413, "docs.error.tooBig"],
    [415, "docs.error.unsupported"],
    [500, "docs.error.generic"],
  ])("explains a %i rejection in plain language and lets the row be dismissed", async (status, key) => {
    backend.reject["raro.xyz"] = status;
    const user = await mountDocuments();
    await screen.findByText(t("docs.empty.title"));
    await user.upload(screen.getByTestId("documents-file-input"), new File(["x"], "raro.xyz"));
    expect(await screen.findByRole("alert")).toHaveTextContent(t(key));
    await user.click(screen.getByRole("button", { name: t("docs.dismiss") }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("refuses a file over 50 MB without calling the backend", async () => {
    const user = await mountDocuments();
    await screen.findByText(t("docs.empty.title"));
    const big = new File(["x"], "enorme.pdf");
    Object.defineProperty(big, "size", { value: 51 * 1024 * 1024 });
    await user.upload(screen.getByTestId("documents-file-input"), big);
    expect(await screen.findByRole("alert")).toHaveTextContent(t("docs.error.tooBig"));
    expect(backend.uploads()).toHaveLength(0);
  });

  it("shows the failure detail of a document and removes it", async () => {
    backend.docs.push({ id: 5, name: "roto.pdf", format: "pdf", size_bytes: 10, status: "failed", status_detail: "PDF cifrado", chunk_count: 0, source_url: "https://x.org/roto.pdf", description: null });
    const user = await mountDocuments();
    const row = within(await screen.findByRole("list", { name: t("docs.list") })).getByRole("listitem");
    expect(row).toHaveTextContent(t("docs.status.failed"));
    expect(row).toHaveTextContent("PDF cifrado");
    expect(row).toHaveTextContent(t("docs.origin.link"));
    await user.click(within(row).getByRole("button", { name: t("docs.removeNamed", { name: "roto.pdf" }) }));
    expect(await screen.findByText(t("docs.empty.title"))).toBeInTheDocument();
    expect(backend.calls.some((c) => c.method === "DELETE" && c.path === "/api/projects/1/documents/5")).toBe(true);
  });
});
