import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale } from "@/i18n";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { StrategyNotices } from "./StrategyNotices";

// SPEC-031 A2, 7: the notice of a strategy that the code reviewer approved, with the two answers of the owner.

const ROW = {
  rule_version_id: 5, rule_id: 2, bank_id: 1, slug: "inside_bar", title: "Inside bar breakout", version: 1, registry_id: "u_inside_bar_v1", stage: "generated",
  origin: { kind: "desk", desk_id: 3, desk_name: "NQ desk" }, summary: "Buys when the close breaks the high of an inside bar.", tests: 6, reviewer: "strategy-reviewer",
};

let calls: Array<{ url: string; method: string; body: unknown; headers: Record<string, string> }> = [];
let listed = [ROW];

function respond(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

beforeEach(() => {
  clearToken();
  setToken(TEST_TOKEN);
  setLocale("en");
  calls = [];
  listed = [ROW];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init: { method?: string; body?: string; headers: Record<string, string> }) => {
      const method = init?.method ?? "GET";
      calls.push({ url, method, body: init?.body ? JSON.parse(init.body) : undefined, headers: init?.headers ?? {} });
      if (url.startsWith("/api/strategy-code?stage=generated")) return respond({ rows: listed });
      if (url === "/api/strategy-code/5") return respond({ registry_id: "u_inside_bar_v1", code: "class InsideBarBreakout:\n    pass", stage: "generated", agent_reviewed: true, owner_reviewed: false });
      if (url.endsWith("/approve") || url.endsWith("/reject")) {
        listed = [];
        return respond({ status: url.endsWith("/approve") ? "owner_reviewed" : "rejected" });
      }
      return respond({}, 404);
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("the notice of a strategy written as code", () => {
  it("says where it was created, what it does and that real money needs the owner", async () => {
    renderWithApp(<StrategyNotices />);
    expect(await screen.findByText(/New strategy ready: Inside bar breakout/)).toBeTruthy();
    expect(screen.getByText(/created in: NQ desk/)).toBeTruthy();
    expect(screen.getByText(/6 tests/)).toBeTruthy();
    expect(screen.getByText(/Real money needs your approval/)).toBeTruthy();
  });

  it("opens the code read only and closes it again", async () => {
    renderWithApp(<StrategyNotices />);
    await userEvent.click(await screen.findByRole("button", { name: "View code" }));
    expect(await screen.findByText(/class InsideBarBreakout/)).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Hide code" }));
    expect(screen.queryByText(/class InsideBarBreakout/)).toBeNull();
  });

  it("asks again before approving, and approving sends the confirmation of the owner's tab", async () => {
    renderWithApp(<StrategyNotices />);
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    expect(screen.getByText("Do you approve this version for real money?")).toBeTruthy();
    expect(calls.some((c) => c.url.endsWith("/approve"))).toBe(false);          // nothing was sent by the first click
    await userEvent.click(screen.getByRole("button", { name: "Yes, approve" }));
    await waitFor(() => expect(screen.getByText(/Inside bar breakout: Approved/)).toBeTruthy());
    const sent = calls.find((c) => c.url === "/api/strategy-code/5/approve")!;
    expect(sent.method).toBe("POST");
    expect(sent.body).toEqual({ confirm: true });
    expect(sent.headers["X-Hokma-Owner"]).toMatch(/^[0-9a-f]{32}$/);
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("cancels the question without sending anything", async () => {
    renderWithApp(<StrategyNotices />);
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
    expect(calls.some((c) => c.url.endsWith("/approve"))).toBe(false);
  });

  it("rejects with an optional note", async () => {
    renderWithApp(<StrategyNotices />);
    await userEvent.click(await screen.findByRole("button", { name: "Reject" }));
    await userEvent.type(screen.getByLabelText("Note (optional)"), "I do not trade breakouts");
    await userEvent.click(screen.getByRole("button", { name: "Reject this version" }));
    await waitFor(() => expect(screen.getByText(/Inside bar breakout: Rejected/)).toBeTruthy());
    expect(calls.find((c) => c.url === "/api/strategy-code/5/reject")!.body).toEqual({ note: "I do not trade breakouts" });
  });

  it("draws nothing when no version waits", async () => {
    listed = [];
    const { container } = renderWithApp(<StrategyNotices />);
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    expect(container.querySelector(".strategy-notices")).toBeNull();
  });
});
