import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchDocument, FetchFailure, listDocuments } from "./documentsApi";

/** The backend answers `{detail: {code, message, reason}}`; the code is the specific one, the reason the interface one. */
function answer(status: number, body: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("fetchDocument against the backend error shape", () => {
  it.each([
    ["link_blocked", 422, "blocked"],
    ["link_not_document", 422, "not_a_document"],
    ["empty_download", 502, "not_a_document"],
    ["timeout", 504, "timeout"],
    ["connect_failed", 502, "network"],
    ["doc_too_large", 413, "too_large"],
    ["doc_duplicate", 409, "duplicate"],
    ["doc_unsupported", 415, "not_a_document"],
  ])("reads reason %s (%i) as %s", async (code, status, reason) => {
    answer(status, { detail: { code, message: "text", reason } });
    const failure = await fetchDocument(1, { url: "https://example.com/a.pdf", title: "a" }).catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(FetchFailure);
    expect((failure as FetchFailure).reason).toBe(reason);
  });

  it("falls back to the status when the body names no reason", async () => {
    answer(409, { detail: { code: "doc_duplicate", message: "x" } });
    const failure = await fetchDocument(1, { url: "https://example.com/a.pdf", title: "" }).catch((e: unknown) => e);
    expect((failure as FetchFailure).reason).toBe("duplicate");
  });
});

describe("listDocuments", () => {
  it("reads the wrapped list the backend returns", async () => {
    answer(200, { documents: [{ id: 3, name: "a.pdf" }] });
    expect((await listDocuments(1)).map((d) => d.id)).toEqual([3]);
  });
});
