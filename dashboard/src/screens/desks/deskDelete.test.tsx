import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderShell } from "@/test/utils";
import { resetDesksStore } from "./store";
import { TODAY_EMPTY } from "./todayFixture";
import type { DeskSummary } from "./types";

function desk(patch: Partial<DeskSummary>): DeskSummary {
  return {
    id: 1, project_id: 10, name: "Mesa A", focus: null, capital: 10000, currency: "USD", status: "active", template: "standard",
    today: 0, drawdown: null, agents_total: 7, agents_working: 0, tokens_week: 0, token_budget: null, budget_state: "ok",
    ...patch,
  };
}

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

interface Call { url: string; method: string }

function stubApi(desks: DeskSummary[], deleteStatus = 200) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push({ url: String(url), method });
      const match = /^\/api\/desks\/(\d+)$/.exec(String(url));
      if (match && method === "DELETE") {
        if (deleteStatus !== 200) return json({ detail: { code: "delete_failed", message: "No se pudo eliminar la mesa." } }, deleteStatus);
        const at = desks.findIndex((d) => String(d.id) === match[1]);
        desks.splice(at, 1);
        return json({ id: Number(match[1]), trash_id: 5 });
      }
      if (match && method === "GET") return json({ id: Number(match[1]), name: "Mesa A", capital: 10000, assets: [] });
      if (url === "/api/desks") return json({ desks });
      if (url === "/api/today") return json(TODAY_EMPTY);
      return json({});
    }),
  );
  return calls;
}

const menu = () => screen.getByRole("complementary", { name: t("nav.menu") });
const openFund = async () => {
  await userEvent.click(within(menu()).getByRole("button", { name: /Mesas de dinero/ }));
  await screen.findByRole("heading", { name: t("desks.fund.tab") });
};
const deletes = (calls: Call[]) => calls.filter((c) => c.method === "DELETE");

beforeEach(() => {
  setLocale("es");
  resetDesksStore();
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("delete a desk", () => {
  it("from the card menu: the button waits for the exact name, then deletes and the card is gone", async () => {
    const calls = stubApi([desk({ id: 1, name: "Mesa A" }), desk({ id: 2, name: "Mesa B" })]);
    renderShell();
    await openFund();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.card.options", { name: "Mesa A" }) }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Eliminar mesa" }));
    const dialog = screen.getByRole("alertdialog", { name: "Eliminar mesa" });
    const confirm = within(dialog).getByRole("button", { name: "Eliminar mesa" });
    expect(confirm).toBeDisabled();
    const field = within(dialog).getByLabelText('Escribe "Mesa A" para confirmar');
    await userEvent.type(field, "mesa a");
    expect(confirm).toBeDisabled();
    await userEvent.clear(field);
    await userEvent.type(field, "Mesa A");
    expect(confirm).toBeEnabled();
    await userEvent.clear(field);
    await userEvent.type(field, "  Mesa A ");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Abrir la mesa Mesa A" })).not.toBeInTheDocument());
    expect(deletes(calls).map((c) => c.url)).toEqual(["/api/desks/1"]);
    expect(screen.getByRole("button", { name: "Abrir la mesa Mesa B" })).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("from Settings: the danger zone deletes the open desk and the app goes back to the Fund", async () => {
    const calls = stubApi([desk({ id: 1, name: "Mesa A" })]);
    renderShell();
    await openFund();
    await userEvent.click(await screen.findByRole("button", { name: "Abrir la mesa Mesa A" }));
    await userEvent.click(await screen.findByRole("tab", { name: t("desks.tab.config") }));
    const zone = await screen.findByRole("region", { name: "Zona de peligro" });
    await userEvent.click(within(zone).getByRole("button", { name: "Eliminar mesa" }));
    const dialog = screen.getByRole("alertdialog", { name: "Eliminar mesa" });
    await userEvent.type(within(dialog).getByLabelText('Escribe "Mesa A" para confirmar'), "Mesa A");
    await userEvent.click(within(dialog).getByRole("button", { name: "Eliminar mesa" }));
    await screen.findByText(t("desks.fund.empty.title"));
    expect(screen.getByRole("heading", { name: t("desks.fund.tab") })).toBeInTheDocument();
    expect(deletes(calls)).toHaveLength(1);
    expect(within(menu()).queryByRole("button", { name: /^Mesa A, / })).not.toBeInTheDocument();
  });

  it("a failed delete keeps the dialog and the desk, and shows the server message", async () => {
    stubApi([desk({ id: 1, name: "Mesa A" })], 500);
    renderShell();
    await openFund();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.card.options", { name: "Mesa A" }) }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Eliminar mesa" }));
    const dialog = screen.getByRole("alertdialog", { name: "Eliminar mesa" });
    await userEvent.type(within(dialog).getByLabelText('Escribe "Mesa A" para confirmar'), "Mesa A");
    await userEvent.click(within(dialog).getByRole("button", { name: "Eliminar mesa" }));
    expect(await within(dialog).findByText("No se pudo eliminar la mesa.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Abrir la mesa Mesa A" })).toBeInTheDocument();
  });
});
