import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { DocumentsSection } from "./DocumentsSection";
import type { DiscoveredItem, DocumentInfo } from "./documentsTypes";

/** In-memory copy of the link part of the documents contract: discover and fetch, plus the list. */
class FakeLinks {
  docs: DocumentInfo[] = [];
  nextId = 1;
  calls: { path: string; body: Record<string, unknown> }[] = [];
  found: DiscoveredItem[] = [];
  pageIsDocument = false;
  /** Item URL to the `reason` the fetch answers with. */
  failures: Record<string, string> = {};
  /** Held fetches: the URL resolves when its gate is released. */
  gates: Record<string, () => void> = {};
  hold = new Set<string>();
  discoverStatus = 200;

  json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

  handle = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://x");
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? (JSON.parse(init.body) as Record<string, unknown>) : {};
    if (method === "GET" && url.pathname === "/api/projects/1/documents") return this.json(this.docs);
    this.calls.push({ path: url.pathname, body });
    if (url.pathname === "/api/projects/1/documents/discover") {
      if (this.discoverStatus !== 200) return this.json({ detail: "bad" }, this.discoverStatus);
      return this.json({ items: this.found, page_is_document: this.pageIsDocument });
    }
    if (url.pathname === "/api/projects/1/documents/fetch") {
      const target = String(body.url);
      if (this.hold.has(target)) await new Promise<void>((resolve) => (this.gates[target] = resolve));
      const reason = this.failures[target];
      if (reason) return this.json({ detail: { code: reason, message: reason } }, reason === "duplicate" ? 409 : 422);
      const doc: DocumentInfo = {
        id: this.nextId++, name: String(body.title ?? target), format: "pdf", size_bytes: 10, status: "ready",
        status_detail: null, chunk_count: 4, source_url: target, description: null,
      };
      this.docs.push(doc);
      return this.json(doc, 201);
    }
    return this.json({ detail: "unexpected" }, 404);
  };

  fetches() {
    return this.calls.filter((c) => c.path.endsWith("/fetch")).map((c) => c.body.url);
  }
}

const item = (n: number, kind: "pdf" | "html" = "pdf"): DiscoveredItem => ({
  url: `https://papers.example/doc${n}.pdf`, title: `Paper ${n}`, kind, host: "papers.example",
});

let backend: FakeLinks;

async function mountSection() {
  setToken("test-session-token-0123456789");
  renderWithApp(<DocumentsSection projectId={1} />);
  const user = userEvent.setup();
  await screen.findByText(t("docs.empty.title"));
  return user;
}

async function search(user: ReturnType<typeof userEvent.setup>, address = "https://papers.example/list") {
  await user.type(screen.getByRole("textbox", { name: t("docs.link.title") }), address);
  await user.click(screen.getByRole("button", { name: t("docs.link.search") }));
}

beforeEach(() => {
  backend = new FakeLinks();
  vi.stubGlobal("fetch", vi.fn(backend.handle));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe.each(["es", "en"] as const)("link screen (%s)", (locale) => {
  beforeEach(() => setLocale(locale));

  it("shows the field, its help and the search button", async () => {
    await mountSection();
    expect(screen.getByRole("textbox", { name: t("docs.link.title") })).toBeInTheDocument();
    expect(screen.getByText(t("docs.link.help"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("docs.link.search") })).toBeDisabled();
  });

  it("refuses an address that is not http or https without calling the backend", async () => {
    const user = await mountSection();
    await search(user, "ftp://papers.example/x");
    expect(await screen.findByRole("alert")).toHaveTextContent(t("docs.link.error.invalid"));
    expect(backend.calls).toHaveLength(0);
  });

  it("lists what it finds with a checkbox per row and counts the marked ones", async () => {
    backend.found = [item(1), item(2), item(3, "html")];
    const user = await mountSection();
    await search(user);
    const list = await screen.findByRole("list", { name: t("docs.link.list") });
    expect(within(list).getAllByRole("checkbox")).toHaveLength(3);
    expect(backend.calls[0]).toEqual({ path: "/api/projects/1/documents/discover", body: { url: "https://papers.example/list" } });
    const download = screen.getByRole("button", { name: t("docs.link.download", { n: 0 }) });
    expect(download).toBeDisabled();
    await user.click(screen.getByRole("button", { name: t("docs.link.markAll") }));
    expect(screen.getByRole("button", { name: t("docs.link.download", { n: 3 }) })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: t("docs.link.clear") }));
    expect(screen.getByRole("button", { name: t("docs.link.download", { n: 0 }) })).toBeDisabled();
    await user.click(within(list).getByRole("checkbox", { name: t("docs.link.select", { name: "Paper 2" }) }));
    expect(screen.getByRole("button", { name: t("docs.link.download", { n: 1 }) })).toBeEnabled();
  });

  it("marks the only item when the address is itself a document", async () => {
    backend.found = [item(1)];
    backend.pageIsDocument = true;
    const user = await mountSection();
    await search(user);
    expect(await screen.findByRole("button", { name: t("docs.link.download", { n: 1 }) })).toBeEnabled();
  });

  it("tells when nothing was found", async () => {
    const user = await mountSection();
    await search(user);
    expect(await screen.findByText(t("docs.link.none"))).toBeInTheDocument();
  });

  it("shows a simple message when the address cannot be read", async () => {
    backend.discoverStatus = 502;
    const user = await mountSection();
    await search(user);
    expect(await screen.findByRole("alert")).toHaveTextContent(t("docs.link.error.search"));
  });

  it("caps the marked rows at 50 per batch", async () => {
    backend.found = Array.from({ length: 60 }, (_, i) => item(i + 1));
    const user = await mountSection();
    await search(user);
    await screen.findByRole("list", { name: t("docs.link.list") });
    await user.click(screen.getByRole("button", { name: t("docs.link.markAll") }));
    expect(screen.getByRole("button", { name: t("docs.link.download", { n: 50 }) })).toBeEnabled();
    await user.click(screen.getByRole("checkbox", { name: t("docs.link.select", { name: "Paper 60" }) }));
    expect(screen.getByRole("button", { name: t("docs.link.download", { n: 50 }) })).toBeEnabled();
  });

  it("downloads the marked rows one at a time with progress per row, and lists them as documents", async () => {
    backend.found = [item(1), item(2)];
    backend.hold.add(item(1).url);
    const user = await mountSection();
    await search(user);
    await screen.findByRole("list", { name: t("docs.link.list") });
    await user.click(screen.getByRole("button", { name: t("docs.link.markAll") }));
    await user.click(screen.getByRole("button", { name: t("docs.link.download", { n: 2 }) }));

    const rows = () => within(screen.getByRole("list", { name: t("docs.link.list") })).getAllByRole("listitem");
    await waitFor(() => expect(rows()[0]).toHaveTextContent(t("docs.link.state.running")));
    expect(rows()[1]).toHaveTextContent(t("docs.link.state.waiting"));
    expect(backend.fetches()).toEqual([item(1).url]);

    backend.gates[item(1).url]();
    await waitFor(() => expect(rows()[0]).toHaveTextContent(t("docs.link.state.done")));
    await waitFor(() => expect(rows()[1]).toHaveTextContent(t("docs.link.state.done")));
    expect(backend.fetches()).toEqual([item(1).url, item(2).url]);
    const added = await screen.findByRole("list", { name: t("docs.list") });
    expect(within(added).getAllByRole("listitem")).toHaveLength(2);
    expect(added).toHaveTextContent(t("docs.origin.link"));
  });

  it("a blocked site gets its simple message and the open-in-browser button, and the rest keep going", async () => {
    backend.found = [item(1), item(2)];
    backend.failures[item(1).url] = "blocked";
    const user = await mountSection();
    await search(user);
    await screen.findByRole("list", { name: t("docs.link.list") });
    await user.click(screen.getByRole("button", { name: t("docs.link.markAll") }));
    await user.click(screen.getByRole("button", { name: t("docs.link.download", { n: 2 }) }));

    const rows = () => within(screen.getByRole("list", { name: t("docs.link.list") })).getAllByRole("listitem");
    await waitFor(() => expect(rows()[1]).toHaveTextContent(t("docs.link.state.done")));
    expect(rows()[0]).toHaveTextContent(t("docs.link.state.failed"));
    expect(rows()[0]).toHaveTextContent(t("docs.link.reason.blocked"));
    const open = within(rows()[0]).getByRole("link", { name: t("docs.link.openNamed", { name: "Paper 1" }) });
    expect(open).toHaveTextContent(t("docs.link.open"));
    expect(open).toHaveAttribute("href", item(1).url);
    expect(open).toHaveAttribute("target", "_blank");
    expect(open).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(within(rows()[1]).queryByRole("link")).not.toBeInTheDocument();
  });

  it.each([
    ["too_large", "docs.link.reason.too_large"],
    ["not_a_document", "docs.link.reason.not_a_document"],
    ["timeout", "docs.link.reason.timeout"],
    ["network", "docs.link.reason.network"],
    ["duplicate", "docs.link.reason.duplicate"],
  ])("maps the reason %s to plain text without the browser button", async (reason, key) => {
    backend.found = [item(1)];
    backend.failures[item(1).url] = reason;
    const user = await mountSection();
    await search(user);
    await screen.findByRole("list", { name: t("docs.link.list") });
    await user.click(screen.getByRole("checkbox", { name: t("docs.link.select", { name: "Paper 1" }) }));
    await user.click(screen.getByRole("button", { name: t("docs.link.download", { n: 1 }) }));
    const row = within(await screen.findByRole("list", { name: t("docs.link.list") })).getByRole("listitem");
    await waitFor(() => expect(row).toHaveTextContent(t(key)));
    expect(within(row).queryByRole("link")).not.toBeInTheDocument();
  });

  it("retries a failed row on the next download", async () => {
    backend.found = [item(1)];
    backend.failures[item(1).url] = "timeout";
    const user = await mountSection();
    await search(user);
    await screen.findByRole("list", { name: t("docs.link.list") });
    await user.click(screen.getByRole("checkbox", { name: t("docs.link.select", { name: "Paper 1" }) }));
    await user.click(screen.getByRole("button", { name: t("docs.link.download", { n: 1 }) }));
    await screen.findByText(t("docs.link.reason.timeout"));
    delete backend.failures[item(1).url];
    await user.click(screen.getByRole("button", { name: t("docs.link.download", { n: 1 }) }));
    await waitFor(() => expect(screen.getByRole("list", { name: t("docs.link.list") })).toHaveTextContent(t("docs.link.state.done")));
    expect(backend.fetches()).toHaveLength(2);
  });
});
