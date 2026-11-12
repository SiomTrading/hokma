import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { getCatalog, setLocale, t } from "@/i18n";
import type { DocumentInfo } from "@/screens/projects/documentsTypes";
import { BUILTIN_SCREENS, SIDEBAR_SCREENS } from "@/shell/screens";
import { useShell } from "@/shell/useShell";
import { renderShell, renderWithApp } from "@/test/utils";
import { LibraryScreen } from "./LibraryScreen";
import { ModelBand } from "./ModelBand";
import type { ModelStatus } from "./modelApi";

/** In-memory copy of the Library contract (SPEC-010 4.8 and 4.4): documents, link download, model state. */
class FakeLibrary {
  docs: DocumentInfo[] = [];
  nextId = 1;
  calls: { method: string; path: string; body: unknown }[] = [];
  reject: Record<string, number> = {};
  model: ModelStatus = { state: "ready", percent: 100, bytes: 1, total: 1 };
  /** Called on retry; returns the state after it. */
  onRetry: () => ModelStatus | null = () => ({ state: "downloading", percent: 0, bytes: 0, total: 100 });
  modelReads = 0;

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
          workdir: "/w", root: "/w/p", active_id: null, kinds: [],
          projects: [{ id: 7, name: "Fondo Cedro", slug: "fondo-cedro", kind: "ips", path: "/w/p/7", active: false, exists: true, files: 0, git: { ok: true, reason: "" } }],
        });
      case "GET /api/library/documents":
        return this.json({ documents: this.docs });
      case "POST /api/library/documents": {
        const form = body as FormData;
        const file = form.get("file") as File;
        const status = this.reject[file.name];
        if (status) return this.json({ detail: { code: "rejected", message: "rejected" } }, status);
        const doc: DocumentInfo = {
          id: this.nextId++, name: file.name, format: file.name.split(".").pop()!, size_bytes: file.size, status: "ready",
          status_detail: null, chunk_count: 3, source_url: null, description: (form.get("description") as string | null) ?? null,
          origin_project_id: null, library_path: "",
        };
        this.docs.push(doc);
        return this.json(doc, 201);
      }
      case "DELETE /api/library/documents/:doc": {
        const id = Number(url.pathname.split("/").pop());
        this.docs = this.docs.filter((d) => d.id !== id);
        return new Response(null, { status: 204 });
      }
      case "POST /api/library/documents/discover":
        return this.json({
          page_is_document: false,
          items: [
            { url: "http://papers.example.test/a.pdf", title: "Paper Alpha", kind: "pdf", host: "papers.example.test" },
            { url: "http://papers.example.test/b.pdf", title: "Paper Beta", kind: "pdf", host: "papers.example.test" },
          ],
        });
      case "POST /api/library/documents/fetch": {
        const sent = JSON.parse(String(body)) as { url: string; title?: string };
        const doc: DocumentInfo = {
          id: this.nextId++, name: `${sent.title}.pdf`, format: "pdf", size_bytes: 10, status: "ready", status_detail: null,
          chunk_count: 2, source_url: sent.url, description: null, origin_project_id: null, library_path: "",
        };
        this.docs.push(doc);
        return this.json(doc, 201);
      }
      case "GET /api/library/folders":
        return this.json({ folders: [] });
      case "GET /api/library/model":
        this.modelReads += 1;
        return this.json(this.model);
      case "POST /api/library/model/retry": {
        if (this.model.state !== "failed") return this.json({ detail: { code: "model_not_failed", message: "no" } }, 409);
        const next = this.onRetry();
        if (next) this.model = next;
        return this.json(this.model);
      }
      default:
        return this.json({ detail: `unexpected ${route}` }, 404);
    }
  };

  uploads() {
    return this.calls.filter((c) => c.method === "POST" && c.path === "/api/library/documents");
  }
}

let backend: FakeLibrary;

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

async function mountLibrary() {
  setToken("test-session-token-0123456789");
  renderWithApp(
    <>
      <LibraryScreen />
      <ToastProbe />
    </>,
  );
  return userEvent.setup();
}

const pdf = (name = "paper.pdf") => new File(["%PDF-1.4"], name, { type: "application/pdf" });
const row = async () => within(await screen.findByRole("list", { name: t("docs.list") })).getByRole("listitem");

beforeEach(() => {
  backend = new FakeLibrary();
  vi.stubGlobal("fetch", vi.fn(backend.handle));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Library menu entry", () => {
  it("sits right after Charts in the sidebar order", () => {
    const list = [...SIDEBAR_SCREENS] as string[];
    expect(list.indexOf("library")).toBe(list.indexOf("chart") + 1);
    expect(BUILTIN_SCREENS.library).toMatchObject({ navKey: "nav.library", titleKey: "screen.library", icon: "library" });
  });

  it.each(["es", "en"] as const)("shows in the left menu next to Charts and opens the screen (%s)", async (locale) => {
    setLocale(locale);
    renderShell();
    const entry = await screen.findByRole("button", { name: t("nav.library") });
    const chart = screen.getByRole("button", { name: t("nav.chart") });
    expect(chart.compareDocumentPosition(entry) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(entry.compareDocumentPosition(screen.getByRole("button", { name: t("nav.connections") })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await userEvent.click(entry);
    expect(await screen.findByTestId("library-screen")).toBeInTheDocument();
  });
});

describe.each(["es", "en"] as const)("Library screen (%s)", (locale) => {
  beforeEach(() => setLocale(locale));

  it("shows the fixed help, the add button, the formats line, the link box and the empty state", async () => {
    await mountLibrary();
    expect(await screen.findByText(t("library.help"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("docs.add") })).toBeInTheDocument();
    expect(screen.getByText(t("docs.formats"))).toBeInTheDocument();
    expect(screen.getByTestId("documents-links")).toBeInTheDocument();
    expect(await screen.findByText(t("library.empty.title"))).toBeInTheDocument();
    expect(backend.calls.some((c) => c.path === "/api/library/documents")).toBe(true);
    expect(backend.calls.some((c) => c.path.startsWith("/api/projects/") && c.path.endsWith("/documents"))).toBe(false);
  });

  it("uploads a picked file and lists it with status, passages and origin", async () => {
    const user = await mountLibrary();
    await screen.findByText(t("library.empty.title"));
    await user.upload(screen.getByTestId("documents-file-input"), pdf());
    const item = await row();
    expect(item).toHaveTextContent("paper.pdf");
    expect(item).toHaveTextContent(t("docs.status.ready"));
    expect(item).toHaveTextContent(t("docs.passages", { n: 3 }));
    expect(item).toHaveTextContent(t("docs.origin.file"));
    expect(screen.getByRole("list", { name: "toasts" })).toHaveTextContent(t("docs.toast.added", { name: "paper.pdf" }));
  });

  it("sends dropped files one by one", async () => {
    await mountLibrary();
    await screen.findByText(t("library.empty.title"));
    const zone = screen.getByRole("group", { name: t("docs.drop.area") });
    fireEvent.drop(zone, { dataTransfer: { files: [pdf("a.pdf"), pdf("b.pdf")], types: ["Files"] } });
    await waitFor(() => expect(within(screen.getByRole("list", { name: t("docs.list") })).getAllByRole("listitem")).toHaveLength(2));
    expect(backend.uploads()).toHaveLength(2);
  });

  it("asks for an optional description of each image before sending it", async () => {
    const user = await mountLibrary();
    await screen.findByText(t("library.empty.title"));
    await user.upload(screen.getByTestId("documents-file-input"), new File(["x"], "grafico.png", { type: "image/png" }));
    expect(backend.uploads()).toHaveLength(0);
    await user.type(screen.getByRole("textbox", { name: t("docs.image.descriptionFor", { name: "grafico.png" }) }), "equity curve");
    await user.click(screen.getByRole("button", { name: t("docs.image.confirm") }));
    expect(await row()).toHaveTextContent("equity curve");
    expect((backend.uploads()[0]!.body as FormData).get("description")).toBe("equity curve");
  });

  it.each([
    [409, "library.error.duplicate"],
    [413, "docs.error.tooBig"],
    [415, "docs.error.unsupported"],
    [500, "docs.error.generic"],
  ])("explains a %i rejection in plain language", async (status, key) => {
    backend.reject["raro.xyz"] = status;
    const user = await mountLibrary();
    await screen.findByText(t("library.empty.title"));
    await user.upload(screen.getByTestId("documents-file-input"), new File(["x"], "raro.xyz"));
    expect(await screen.findByRole("alert")).toHaveTextContent(t(key));
    await user.click(screen.getByRole("button", { name: t("docs.dismiss") }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("labels a document that came from a project and removes it", async () => {
    backend.docs.push({
      id: 5, name: "viejo.pdf", format: "pdf", size_bytes: 10, status: "ready", status_detail: null, chunk_count: 1,
      source_url: null, description: null, origin_project_id: 7, library_path: "",
    });
    backend.docs.push({
      id: 6, name: "otro.pdf", format: "pdf", size_bytes: 10, status: "ready", status_detail: null, chunk_count: 1,
      source_url: null, description: null, origin_project_id: null, library_path: "",
    });
    const user = await mountLibrary();
    const items = within(await screen.findByRole("list", { name: t("docs.list") })).getAllByRole("listitem");
    await waitFor(() => expect(items[0]).toHaveTextContent(t("library.origin.from", { project: "Fondo Cedro" })));
    expect(items[1]).not.toHaveTextContent(t("library.origin.from", { project: "Fondo Cedro" }));
    await user.click(within(items[0]!).getByRole("button", { name: t("docs.removeNamed", { name: "viejo.pdf" }) }));
    await waitFor(() => expect(within(screen.getByRole("list", { name: t("docs.list") })).getAllByRole("listitem")).toHaveLength(1));
    expect(backend.calls.some((c) => c.method === "DELETE" && c.path === "/api/library/documents/5")).toBe(true);
  });

  it("finds documents at a link, marks them and downloads them into the Library", async () => {
    const user = await mountLibrary();
    await screen.findByText(t("library.empty.title"));
    const links = screen.getByTestId("documents-links");
    await user.type(within(links).getByLabelText(t("docs.link.title")), "http://papers.example.test/list");
    await user.click(within(links).getByRole("button", { name: t("docs.link.search") }));
    await within(links).findByText("Paper Alpha");
    await user.click(within(links).getByRole("button", { name: t("docs.link.markAll") }));
    await user.click(within(links).getByRole("button", { name: t("docs.link.download", { n: 2 }) }));
    await waitFor(() => expect(within(screen.getByRole("list", { name: t("docs.list") })).getAllByRole("listitem")).toHaveLength(2));
    expect(backend.calls.filter((c) => c.path === "/api/library/documents/fetch")).toHaveLength(2);
    expect(backend.calls.filter((c) => c.path === "/api/library/documents/discover")).toHaveLength(1);
  });

  it("keeps reading the list while a document is still indexing, then shows it ready", async () => {
    backend.docs.push({
      id: 3, name: "lento.pdf", format: "pdf", size_bytes: 10, status: "indexing", status_detail: null, chunk_count: 0,
      source_url: null, description: null, origin_project_id: null, library_path: "",
    });
    await mountLibrary();
    expect(await row()).toHaveTextContent(t("docs.status.indexing"));
    backend.docs[0] = { ...backend.docs[0]!, status: "ready", chunk_count: 4 };
    await waitFor(async () => expect(await row()).toHaveTextContent(t("docs.status.ready")), { timeout: 6000 });
  }, 10000);

  it("shows no search note when the model is ready", async () => {
    await mountLibrary();
    await screen.findByText(t("library.empty.title"));
    await waitFor(() => expect(backend.modelReads).toBeGreaterThan(0));
    expect(screen.queryByText(t("library.model.preparing"))).not.toBeInTheDocument();
    expect(screen.queryByTestId("library-model-preparing")).not.toBeInTheDocument();
    expect(screen.queryByTestId("library-model-failed")).not.toBeInTheDocument();
  });
});

describe.each(["es", "en"] as const)("search preparation band (%s)", (locale) => {
  beforeEach(() => {
    setLocale(locale);
    setToken("test-session-token-0123456789");
  });

  const mountBand = () => render(<ModelBand pollMs={15} />);

  it("shows a progress bar and the preparing note, with no button, while downloading", async () => {
    backend.model = { state: "downloading", percent: 42, bytes: 42, total: 100 };
    mountBand();
    const band = await screen.findByTestId("library-model-preparing");
    expect(band).toHaveTextContent(t("library.model.preparing"));
    expect(within(band).getByRole("progressbar", { name: t("library.model.progress") })).toHaveAttribute("aria-valuenow", "42");
    expect(within(band).queryByRole("button")).not.toBeInTheDocument();
  });

  it("disappears without a trace when the model becomes ready, and stops reading", async () => {
    backend.model = { state: "downloading", percent: 90, bytes: 90, total: 100 };
    mountBand();
    await screen.findByTestId("library-model-preparing");
    backend.model = { state: "ready", percent: 100, bytes: 100, total: 100 };
    await waitFor(() => expect(screen.queryByTestId("library-model-preparing")).not.toBeInTheDocument());
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    const reads = backend.modelReads;
    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(backend.modelReads).toBe(reads);
  });

  it("offers a single Retry button when it failed, and goes back to preparing after pressing it", async () => {
    backend.model = { state: "failed", percent: 10, bytes: 10, total: 100 };
    mountBand();
    const band = await screen.findByTestId("library-model-failed");
    expect(band).toHaveTextContent(t("library.model.failed"));
    const buttons = within(band).getAllByRole("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveTextContent(t("library.model.retry"));
    await userEvent.click(buttons[0]!);
    expect(await screen.findByTestId("library-model-preparing")).toBeInTheDocument();
    expect(backend.calls.filter((c) => c.method === "POST" && c.path === "/api/library/model/retry")).toHaveLength(1);
  });

  it("reads the real state when Retry is answered with 409", async () => {
    backend.model = { state: "failed", percent: 0, bytes: 0, total: 100 };
    mountBand();
    await screen.findByTestId("library-model-failed");
    backend.model = { state: "ready", percent: 100, bytes: 100, total: 100 };
    await userEvent.click(screen.getByRole("button", { name: t("library.model.retry") }));
    await waitFor(() => expect(screen.queryByTestId("library-model-failed")).not.toBeInTheDocument());
  });

  it("shows nothing when the app cannot answer", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 500 })));
    mountBand();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByTestId("library-model-preparing")).not.toBeInTheDocument();
    expect(screen.queryByTestId("library-model-failed")).not.toBeInTheDocument();
  });
});

describe("no activation anywhere", () => {
  const FORBIDDEN = /activ|enable|turn on|descargar modelo|download model|cancel|desactiv|interruptor|toggle/i;

  it.each(["es", "en"] as const)("the Library texts never offer to turn the search on (%s)", (locale) => {
    const catalog = getCatalog(locale);
    for (const [key, text] of Object.entries(catalog)) {
      if (key.startsWith("library.") || key === "nav.library" || key === "screen.library") {
        expect(text, key).not.toMatch(FORBIDDEN);
      }
    }
  });

  it.each(["es", "en"] as const)("the screen with the model downloading has no switch and no activation button (%s)", async (locale) => {
    setLocale(locale);
    backend.model = { state: "downloading", percent: 5, bytes: 5, total: 100 };
    await mountLibrary();
    await screen.findByTestId("library-model-preparing");
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    for (const button of screen.getAllByRole("button")) expect(button.textContent ?? "").not.toMatch(FORBIDDEN);
  });
});
