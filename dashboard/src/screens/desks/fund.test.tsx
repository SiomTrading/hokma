import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { setChatStart, takeChatStart } from "@/screens/chat/start";
import { renderShell } from "@/test/utils";
import { resetDesksStore } from "./store";
import type { DeskSummary, DeskTemplateDef } from "./types";

function desk(patch: Partial<DeskSummary>): DeskSummary {
  return {
    id: 1, project_id: 10, name: "Mesa A", focus: null, capital: 10000, currency: "USD", status: "active", template: "standard",
    today: 0, drawdown: null, agents_total: 7, agents_working: 0, tokens_week: 0, token_budget: null, budget_state: "ok",
    ...patch,
  };
}

const TEMPLATES: DeskTemplateDef[] = [
  { id: "minimal", token_budget: 150000, agents: [{ slug: "desk-chief", role: "chief", model: "sonnet" }, { slug: "hugo", role: "data", model: "haiku" }, { slug: "risk", role: "risk", model: "haiku" }] },
  { id: "standard", token_budget: 300000, agents: [{ slug: "desk-chief", role: "chief", model: "sonnet" }, { slug: "hugo", role: "data", model: "haiku" }, { slug: "risk", role: "risk", model: "haiku" }, { slug: "iris", role: "research", model: "sonnet" }] },
  { id: "debate", token_budget: 600000, agents: [{ slug: "desk-chief", role: "chief", model: "sonnet" }, { slug: "judge", role: "judge", model: "sonnet" }] },
];

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
/** The long dash, built by code so this file stays free of it. */
const DASH = String.fromCharCode(0x2014);

interface Call { url: string; method: string; body: unknown }

function stubApi(desks: DeskSummary[], created: { id: number } | Response = { id: 99 }) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
      calls.push({ url: String(url), method, body });
      if (url === "/api/desks/templates") return json({ templates: TEMPLATES });
      if (url === "/api/desks" && method === "POST") {
        if (created instanceof Response) return created;
        desks.push(desk({ id: created.id, name: String((body as { name?: string }).name) }));
        return json(created, 201);
      }
      if (url === "/api/desks") return json({ desks });
      return json({});
    }),
  );
  return calls;
}

const menu = () => screen.getByRole("complementary", { name: t("nav.menu") });
const openFund = async () => {
  await userEvent.click(within(menu()).getByRole("button", { name: /Mesas de dinero/ }));
  await screen.findByRole("heading", { name: "Mesas de dinero" });
};

beforeEach(() => {
  setLocale("es");
  resetDesksStore();
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the Fund", () => {
  it("shows one card per desk with its state, capital, result of the day, drawdown and agents working", async () => {
    stubApi([
      desk({ id: 1, name: "Futuros NQ", focus: "Intraday futures", capital: 10000, agents_working: 2 }),
      desk({ id: 2, name: "Swing", capital: 25000, status: "paused", token_budget: 1000, tokens_week: 850, budget_state: "warn" }),
    ]);
    renderShell();
    await openFund();
    const first = await screen.findByRole("button", { name: "Abrir la mesa Futuros NQ" });
    expect(first).toHaveTextContent("Intraday futures");
    expect(first).toHaveTextContent("Activa");
    expect(first).toHaveTextContent("10.000");
    expect(first).toHaveTextContent("0,00");
    expect(first).toHaveTextContent("Sin datos");
    expect(first).toHaveTextContent("2 de 7 agentes trabajando");
    expect(first).toHaveTextContent(t("desks.mode.simulation"));
    const second = screen.getByRole("button", { name: "Abrir la mesa Swing" });
    expect(second).toHaveTextContent("En pausa");
    expect(second).toHaveTextContent("Presupuesto al 80%");
  });

  it("adds up the capital of the desks in the header and shows the empty curve and activity", async () => {
    stubApi([desk({ id: 1, capital: 10000, tokens_week: 100 }), desk({ id: 2, name: "B", capital: 25000, tokens_week: 50 })]);
    renderShell();
    await openFund();
    await screen.findByRole("button", { name: "Abrir la mesa Mesa A" });
    expect(screen.getByText("35.000")).toBeInTheDocument();
    expect(screen.getByText("150")).toBeInTheDocument();
    expect(screen.getAllByText(t("desks.fund.curve.empty.text"))).toHaveLength(2);
    expect(screen.getByRole("region", { name: "Curva del fondo" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Actividad de las mesas" })).toBeInTheDocument();
  });

  it("keeps 'Lecciones del fondo' disabled and says when it arrives", async () => {
    stubApi([]);
    renderShell();
    await openFund();
    const lessons = await screen.findByRole("tab", { name: "Lecciones del fondo" });
    expect(lessons).toBeDisabled();
    expect(screen.getAllByText("Llega en el paso 6").length).toBeGreaterThan(0);
  });

  it("opens a desk when its card is clicked", async () => {
    stubApi([desk({ id: 5, name: "Futuros NQ" })]);
    renderShell();
    await openFund();
    await userEvent.click(await screen.findByRole("button", { name: "Abrir la mesa Futuros NQ" }));
    expect(await screen.findByRole("tablist", { name: "Secciones de la mesa" })).toBeInTheDocument();
  });
});

describe("creating a desk", () => {
  it("asks for the two paths in Spanish", async () => {
    stubApi([]);
    renderShell();
    await openFund();
    await userEvent.click(await screen.findByRole("button", { name: "+ Crear mesa" }));
    const dialog = await screen.findByRole("dialog", { name: t("desks.start.title") });
    expect(within(dialog).getByText(t("desks.start.hokma.name"))).toBeInTheDocument();
    expect(within(dialog).getByText("Recomendado")).toBeInTheDocument();
    expect(within(dialog).getByText("Desde una plantilla")).toBeInTheDocument();
  });

  it("creates a Standard desk with POST /api/desks and the expected payload, in 4 clicks, and opens it", async () => {
    const calls = stubApi([]);
    renderShell();
    await openFund();
    let clicks = 0;
    const click = async (el: HTMLElement) => {
      clicks += 1;
      await userEvent.click(el);
    };
    await click(await screen.findByRole("button", { name: "+ Crear mesa" }));
    await click(await screen.findByRole("button", { name: new RegExp(`^${t("desks.start.template.name")}`) }));
    await userEvent.type(await screen.findByLabelText("Nombre de la mesa"), "Futuros NQ");
    await userEvent.type(screen.getByLabelText("Enfoque"), "Intraday futures");
    await click(screen.getByRole("button", { name: "Siguiente: equipo" }));
    expect(await screen.findByText("Jefe de mesa · Sonnet")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: new RegExp(t("desks.tpl.standard.name")) })).toHaveAttribute("aria-checked", "true");
    await click(screen.getByRole("button", { name: "Crear mesa" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.url).toBe("/api/desks");
    expect(post.body).toEqual({ name: "Futuros NQ", focus: "Intraday futures", capital: 10000, template: "standard", assets: [], token_budget: 300000 });
    expect(clicks).toBeLessThan(6);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(calls.filter((c) => c.url === "/api/desks" && c.method === "GET").length).toBeGreaterThan(1);
    expect(await screen.findByRole("tablist", { name: "Secciones de la mesa" })).toBeInTheDocument();
  });

  it("sends the picked template, the assets, the goal and no budget when the cap is off", async () => {
    const calls = stubApi([]);
    renderShell();
    await openFund();
    await userEvent.click(await screen.findByRole("button", { name: "+ Crear mesa" }));
    await userEvent.click(await screen.findByRole("button", { name: new RegExp(`^${t("desks.start.template.name")}`) }));
    await userEvent.type(await screen.findByLabelText("Nombre de la mesa"), "Swing");
    await userEvent.clear(screen.getByLabelText("Capital simulado"));
    await userEvent.type(screen.getByLabelText("Capital simulado"), "25.000");
    await userEvent.type(screen.getByLabelText("Objetivo mensual (%)"), "2,5");
    await userEvent.type(screen.getByLabelText("Mandato"), "Sin operar en resultados");
    await userEvent.click(screen.getByRole("button", { name: "Siguiente: equipo" }));
    await userEvent.click(await screen.findByRole("radio", { name: /Con debate/ }));
    expect(screen.getByText(t("desks.tpl.debate.horizon"))).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Siguiente: reglas" }));
    await userEvent.click(screen.getByRole("switch", { name: "Poner un tope semanal de tokens" }));
    expect(screen.getByText(t("desks.rules.title"))).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Siguiente: activos y datos" }));
    await userEvent.type(screen.getByLabelText(t("desks.field.asset")), "nvda{Enter}");
    await userEvent.type(screen.getByLabelText(t("desks.field.asset")), "MSFT{Enter}");
    await userEvent.click(screen.getByRole("button", { name: "Quitar MSFT" }));
    await userEvent.click(screen.getByRole("button", { name: "Crear mesa" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({
      name: "Swing", focus: null, capital: 25000, objective_pct: 2.5, mandate: "Sin operar en resultados", template: "debate", assets: ["NVDA"],
    });
  });

  it("does not move on without a name and does not create anything", async () => {
    const calls = stubApi([]);
    renderShell();
    await openFund();
    await userEvent.click(await screen.findByRole("button", { name: "+ Crear mesa" }));
    await userEvent.click(await screen.findByRole("button", { name: new RegExp(`^${t("desks.start.template.name")}`) }));
    await userEvent.click(await screen.findByRole("button", { name: "Siguiente: equipo" }));
    expect(await screen.findByText("Este dato es obligatorio.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Crear mesa" })).not.toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("shows the server message when the creation fails and keeps the assistant open", async () => {
    stubApi([], json({ detail: { code: "desk.name_taken", message: "Ya existe una mesa con ese nombre." } }, 409));
    renderShell();
    await openFund();
    await userEvent.click(await screen.findByRole("button", { name: "+ Crear mesa" }));
    await userEvent.click(await screen.findByRole("button", { name: new RegExp(`^${t("desks.start.template.name")}`) }));
    await userEvent.type(await screen.findByLabelText("Nombre de la mesa"), "Repetida");
    await userEvent.click(screen.getByRole("button", { name: "Siguiente: equipo" }));
    await screen.findByText("Jefe de mesa · Sonnet");
    await userEvent.click(screen.getByRole("button", { name: "Crear mesa" }));
    const dialog = await screen.findByRole("dialog", { name: "Nueva mesa" });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Ya existe una mesa con ese nombre.");
  });

  it("'Create it with Hokma' opens the chat with the first message, creates nothing and says the template creates it", async () => {
    const calls = stubApi([]);
    renderShell();
    await openFund();
    await userEvent.click(await screen.findByRole("button", { name: "+ Crear mesa" }));
    await userEvent.click(await screen.findByRole("button", { name: new RegExp(`^${t("desks.start.hokma.name")}`) }));
    await waitFor(() => {
      const boxes = screen.getAllByPlaceholderText(t("chat.composer.placeholder")) as HTMLTextAreaElement[];
      expect(boxes.some((box) => box.value.includes("mesa de dinero"))).toBe(true);
    });
    expect(await screen.findByText(t("desks.hokma.toast"))).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens the flow right away when the menu asks for a new desk", async () => {
    stubApi([desk({ id: 1 })]);
    renderShell();
    await userEvent.click(await within(menu()).findByRole("button", { name: "Nueva mesa" }));
    expect(await screen.findByRole("dialog", { name: t("desks.start.title") })).toBeInTheDocument();
  });

  it("reads in English too", async () => {
    setLocale("en");
    stubApi([]);
    renderShell();
    await userEvent.click(within(menu()).getByRole("button", { name: /Money desks/ }));
    await userEvent.click(await screen.findByRole("button", { name: "+ Create desk" }));
    const dialog = await screen.findByRole("dialog", { name: "How do you want to create the desk?" });
    expect(within(dialog).getByText("Create it with Hokma")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: new RegExp(`^${t("desks.start.template.name")}`) }));
    expect(await screen.findByLabelText("Desk name")).toBeInTheDocument();
    expect(screen.getByText("It starts in simulation. Real money stays blocked.")).toBeInTheDocument();
  });
});

describe("handing a first message to a new chat", () => {
  it("gives the text once to its own draft and to nobody else", () => {
    setChatStart("d1", "hello");
    expect(takeChatStart("other")).toBeNull();
    expect(takeChatStart("d1")).toBe("hello");
    expect(takeChatStart("d1")).toBeNull();
    expect(takeChatStart(null)).toBeNull();
  });

  it("has no long dash in the texts of the creation flow", async () => {
    const { getCatalog } = await import("@/i18n");
    for (const locale of ["es", "en"] as const) {
      const catalog = getCatalog(locale);
      for (const key of Object.keys(catalog).filter((k) => k.startsWith("desks."))) expect(catalog[key]!.includes(DASH), key).toBe(false);
    }
  });
});
