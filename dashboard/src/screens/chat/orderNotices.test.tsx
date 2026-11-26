import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/api";
import { t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { ChatHub, setHub } from "./hub";
import { NoticeStore } from "./orderNotices";
import type { CapNotice, OrderNotice } from "./orderNotices";
import { NoticeActions, OrderNoticeCards, noticeMinutesLeft } from "./OrderNoticeCards";

const notice = (id: number, patch: Partial<OrderNotice> = {}): OrderNotice => ({
  proposal_id: id, desk_id: 1, desk: "Swing", account: "Sim A", rules: ["daily_loss"], whys: ["loss 61 over 60"], timeout_min: 30,
  expires_at: new Date(Date.now() + 25 * 60000).toISOString().replace("T", " ").slice(0, 19), stage: "ask", order: "BUY 1 NQ", summary: "BUY 1 FUT:NQ:2026-12", ...patch,
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  setHub(null);
});

describe("NoticeStore", () => {
  it("adds a card, replaces the same order and removes it when the notice closes; the count is what turns the strip amber", () => {
    const store = new NoticeStore();
    const seen = vi.fn();
    store.subscribe(seen);
    store.apply({ type: "order_notice", ...notice(1) });
    store.apply({ type: "order_notice", ...notice(2) });
    store.apply({ type: "order_notice", ...notice(1, { whys: ["new"] }) });
    expect(store.count()).toBe(2);
    expect(store.get().find((n) => n.proposal_id === 1)?.whys).toEqual(["new"]);
    store.apply({ type: "order_notice_closed", proposal_id: 1, outcome: "expired" });
    expect(store.get().map((n) => n.proposal_id)).toEqual([2]);
    store.apply({ type: "order_notice_closed", proposal_id: 99, outcome: "no" });
    expect(seen).toHaveBeenCalledTimes(4);
  });

  it("refresh takes the truth of the server and keeps what it has when the call fails", async () => {
    const store = new NoticeStore();
    vi.spyOn(api, "get").mockResolvedValueOnce({ notices: [notice(5)] });
    await store.refresh();
    expect(store.count()).toBe(1);
    vi.spyOn(api, "get").mockRejectedValueOnce(new Error("down"));
    await store.refresh();
    expect(store.count()).toBe(1);
  });
});

describe("the hub", () => {
  it("routes the notice events to its store and never to a conversation tab", () => {
    let handler: ((data: unknown) => void) | null = null;
    const hub = new ChatHub((() => ({ send: () => true, close: () => undefined })) as never);
    (hub as unknown as { socket: unknown }).socket = { send: () => true, close: () => undefined };
    const tab = { id: null, awaiting: true, onEvent: vi.fn(), onOpen: vi.fn(), claim: vi.fn() };
    hub.register(tab);
    (hub as unknown as { route: (d: unknown) => void }).route({ type: "order_notice", ...notice(7) });
    expect(hub.notices.count()).toBe(1);
    expect(tab.onEvent).not.toHaveBeenCalled();
    (hub as unknown as { route: (d: unknown) => void }).route({ type: "order_notice_closed", proposal_id: 7, outcome: "followed" });
    expect(hub.notices.count()).toBe(0);
    expect(handler).toBeNull();
  });
});

describe("the card of the chat", () => {
  it("shows the rule, why, the order and that nothing is executed without an answer, with the three buttons", () => {
    const hub = new ChatHub((() => ({ send: () => true, close: () => undefined })) as never);
    (hub as unknown as { socket: unknown }).socket = { send: () => true, close: () => undefined };
    setHub(hub);
    hub.notices.apply({ type: "order_notice", ...notice(3) });
    renderWithApp(<OrderNoticeCards />);
    expect(screen.getByText(/daily_loss/)).toBeTruthy();
    expect(screen.getByText("loss 61 over 60")).toBeTruthy();
    expect(screen.getByText(t("chat.notice.neverSent"))).toBeTruthy();
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual([t("chat.notice.follow"), t("chat.notice.no"), t("chat.notice.close")]);
  });

  it("counts minutes left from the deadline and treats a missing deadline as ended", () => {
    const now = Date.parse("2026-11-25T10:00:00Z");
    expect(noticeMinutesLeft("2026-11-25 10:07:30", now)).toBe(8);
    expect(noticeMinutesLeft("2026-11-25 09:59:00", now)).toBeLessThanOrEqual(0);
    expect(noticeMinutesLeft(null, now)).toBe(0);
  });
});

describe("the answers", () => {
  const renderActions = (onDone = vi.fn()) => renderWithApp(<NoticeActions proposalId={9} label="Swing" onDone={onDone} />);

  it("Follow asks the scope every time and sends the chosen one; the scope question has no way to follow without choosing", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({ status: "sent" });
    const onDone = vi.fn();
    renderActions(onDone);
    await userEvent.click(screen.getByRole("button", { name: t("chat.notice.follow") }));
    expect(post).not.toHaveBeenCalled();                                         // Follow alone sends nothing: the scope comes first
    expect(screen.queryByRole("button", { name: t("chat.notice.close") })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: t("chat.notice.restOfDay") }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/api/orders/notices/9/follow", { scope: "day" }));
    expect(onDone).toHaveBeenCalled();
  });

  it("only this order sends scope order; No and Close have their own routes", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({ status: "ok" });
    renderActions();
    await userEvent.click(screen.getByRole("button", { name: t("chat.notice.follow") }));
    await userEvent.click(screen.getByRole("button", { name: t("chat.notice.onlyThis") }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/api/orders/notices/9/follow", { scope: "order" }));
    cleanup();
    renderActions();
    await userEvent.click(screen.getByRole("button", { name: t("chat.notice.no") }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/api/orders/notices/9/no", {}));
    cleanup();
  });

  it("Close only asks: it shows the list and sends nothing to close until the confirm button, with the token of that list", async () => {
    const post = vi.spyOn(api, "post").mockImplementation(async (path: string) =>
      path.endsWith("/close") ? { status: "close_confirm", proposal_id: 9, plan_token: "abc123def456", text: "- SELL 1 FUT:NQ:2026-12 (Swing)" } : { status: "rejected_flat" });
    const onDone = vi.fn();
    renderActions(onDone);
    await userEvent.click(screen.getByRole("button", { name: t("chat.notice.close") }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/api/orders/notices/9/close", {}));
    expect(await screen.findByText(/SELL 1 FUT:NQ:2026-12/)).toBeTruthy();
    expect(post).not.toHaveBeenCalledWith("/api/orders/notices/9/close/confirm", expect.anything());
    expect(onDone).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: t("chat.notice.closeBack") }));          // going back closes nothing
    expect(screen.getByRole("button", { name: t("chat.notice.close") })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: t("chat.notice.close") }));
    await userEvent.click(await screen.findByRole("button", { name: t("chat.notice.closeConfirm") }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/api/orders/notices/9/close/confirm", { plan_token: "abc123def456" }));
    expect(onDone).toHaveBeenCalled();
  });

  it("shows the reason when the answer is refused (for example it expired) and does not close the card by itself", async () => {
    vi.spyOn(api, "post").mockRejectedValue(new ApiError(409, "decision_expired", "That order request expired"));
    const onDone = vi.fn();
    renderActions(onDone);
    await userEvent.click(screen.getByRole("button", { name: t("chat.notice.no") }));
    expect((await screen.findByRole("alert")).textContent).toContain("expired");
    expect(onDone).not.toHaveBeenCalled();
  });
});


const cap = (id: number, patch: Partial<CapNotice> = {}): CapNotice => ({
  cap_id: id, account_id: 1, account: "Sim A", desk: "Swing", rule_id: "a1:daily_loss", control: "daily_loss", metric: "daily_loss", value: "500.00", cap: "500.00",
  read_at: "2026-11-25 15:00:00", timeout_min: 30, expires_at: new Date(Date.now() + 25 * 60000).toISOString().replace("T", " ").slice(0, 19), stage: "ask", ...patch,
});

describe("the notice of a cap with no order", () => {
  it("the store adds and removes it, counts it for the amber strip, and the hub routes it to the store and not to a tab", () => {
    const store = new NoticeStore();
    store.apply({ type: "cap_notice", ...cap(4) });
    store.apply({ type: "cap_notice", ...cap(4, { value: "510.00" }) });
    expect(store.count()).toBe(1);
    expect(store.getCaps()[0].value).toBe("510.00");
    store.apply({ type: "cap_notice_closed", cap_id: 4, outcome: "expired" });
    expect(store.count()).toBe(0);
    const hub = new ChatHub((() => ({ send: () => true, close: () => undefined })) as never);
    (hub as unknown as { socket: unknown }).socket = { send: () => true, close: () => undefined };
    const tab = { id: null, awaiting: true, onEvent: vi.fn(), onOpen: vi.fn(), claim: vi.fn() };
    hub.register(tab);
    (hub as unknown as { route: (d: unknown) => void }).route({ type: "cap_notice", ...cap(9) });
    expect(hub.notices.count()).toBe(1);
    expect(tab.onEvent).not.toHaveBeenCalled();
  });

  it("the card says what was measured and has the three buttons; each one calls its route", async () => {
    const hub = new ChatHub((() => ({ send: () => true, close: () => undefined })) as never);
    (hub as unknown as { socket: unknown }).socket = { send: () => true, close: () => undefined };
    setHub(hub);
    hub.notices.apply({ type: "cap_notice", ...cap(3) });
    const post = vi.spyOn(api, "post").mockResolvedValue({ status: "followed" });
    renderWithApp(<OrderNoticeCards />);
    expect(screen.getByText(/a1:daily_loss/)).toBeTruthy();
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual([t("chat.cap.follow"), t("chat.cap.block"), t("chat.cap.close")]);
    await userEvent.click(screen.getByRole("button", { name: t("chat.cap.follow") }));
    expect(post).toHaveBeenCalledWith("/api/orders/cap-notices/3/follow", {});
    await userEvent.click(screen.getByRole("button", { name: t("chat.cap.block") }));
    expect(post).toHaveBeenCalledWith("/api/orders/cap-notices/3/block", {});
  });

  it("Close positions shows the list first and sends nothing until it is confirmed with its token", async () => {
    const hub = new ChatHub((() => ({ send: () => true, close: () => undefined })) as never);
    (hub as unknown as { socket: unknown }).socket = { send: () => true, close: () => undefined };
    setHub(hub);
    hub.notices.apply({ type: "cap_notice", ...cap(6) });
    const post = vi.spyOn(api, "post").mockResolvedValueOnce({ status: "close_confirm", plan_token: "abc", text: "- SELL 1 FUT:NQ" }).mockResolvedValue({ status: "closed" });
    renderWithApp(<OrderNoticeCards />);
    await userEvent.click(screen.getByRole("button", { name: t("chat.cap.close") }));
    expect(screen.getByText("- SELL 1 FUT:NQ")).toBeTruthy();
    expect(post).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: t("chat.notice.closeConfirm") }));
    expect(post).toHaveBeenLastCalledWith("/api/orders/cap-notices/6/close/confirm", { plan_token: "abc" });
  });
});
