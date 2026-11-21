import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { getCatalog, setLocale, t } from "@/i18n";
import type { Locale } from "@/i18n";
import { installFakeSockets } from "@/test/fakeSocket";
import { BAD_KEY_TEST, FakeConnections, SECRET } from "@/test/connectionsBackend";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { ConnectionsScreen } from "./ConnectionsScreen";

let backend: FakeConnections;

/** The guide of each provider (7.4): how many steps it has, and which parts it shows. Gmail reuses mail.help; SiomTrading Futures waits for its cost and steps. */
const GUIDES = [
  { id: "schwab", name: "Schwab", steps: 8, cost: true, fail: true },
  { id: "alpaca", name: "Alpaca IEX", steps: 3, cost: true, fail: true },
  { id: "finnhub", name: "Finnhub", steps: 3, cost: true, fail: true },
  { id: "siomtrading_futures", name: "SiomTrading Futures", steps: 1, cost: true, fail: true },
  { id: "coinbase", name: "Coinbase", steps: 1, cost: true, fail: true },
] as const;

async function show() {
  const view = renderWithApp(<ConnectionsScreen />);
  await screen.findByTestId("connection-coinbase");
  return view;
}

async function pick(name: string) {
  await userEvent.click(screen.getByRole("button", { name: t("connections.add") }));
  const list = await screen.findByRole("dialog", { name: t("connections.add.title") });
  await userEvent.click(within(list).getByRole("button", { name: t("connections.add.choose"), description: name }));
}

const guide = () => screen.getByTestId("provider-guide");

beforeEach(() => {
  backend = new FakeConnections();
  clearToken();
  setToken(TEST_TOKEN);
  setLocale("en");
  installFakeSockets();
  vi.stubGlobal("fetch", vi.fn(backend.handle));
  Object.assign(backend.find("coinbase"), { added: false, enabled: false, state: "not_connected" });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the guide of each provider, in Spanish and in English", () => {
  for (const locale of ["es", "en"] as Locale[]) {
    for (const spec of GUIDES) {
      it(`${spec.id} shows what it gives, the cost, the steps and what to do if the test fails (${locale})`, async () => {
        setLocale(locale);
        await show().catch(() => undefined);
        await pick(spec.name);
        const block = within(guide());
        expect(block.getByText(t("connections.guide.title"))).toBeInTheDocument();
        expect(block.getByTestId("guide-what")).toHaveTextContent(t("connections.guide." + spec.id + ".what"));
        expect(block.getByTestId("guide-what")).toHaveTextContent(t("connections.guide.what"));
        if (spec.cost) {
          expect(block.getByTestId("guide-cost")).toHaveTextContent(t("connections.guide." + spec.id + ".cost"));
          expect(block.getByTestId("guide-cost")).toHaveTextContent(t("connections.guide.cost"));
        } else {
          expect(block.queryByTestId("guide-cost")).toBeNull();
        }
        if (spec.steps > 0) {
          const steps = within(block.getByTestId("guide-steps")).getAllByRole("listitem");
          expect(steps).toHaveLength(spec.steps);
          steps.forEach((li, index) => expect(li).toHaveTextContent(t("connections.guide." + spec.id + ".steps." + (index + 1)), { normalizeWhitespace: true }));
          expect(block.getByTestId("guide-steps")).toHaveTextContent(t("connections.guide.steps"));
        } else {
          expect(block.queryByTestId("guide-steps")).toBeNull();
        }
        expect(block.getByTestId("guide-fail-block")).toHaveTextContent(t("connections.guide." + spec.id + ".fail"));
        expect(block.getByTestId("guide-fail-block")).toHaveTextContent(t("connections.guide.fail"));
      });
    }

    it(`Gmail shows its guide and the text mail.help once (${locale})`, async () => {
      setLocale(locale);
      await show().catch(() => undefined);
      await pick(t("mail.title"));
      const block = within(guide());
      expect(block.getByTestId("guide-what")).toHaveTextContent(t("connections.guide.mail.what"));
      expect(block.getByTestId("guide-cost")).toHaveTextContent(t("connections.guide.mail.cost"));
      expect(within(block.getByTestId("guide-steps")).getAllByRole("listitem")[0]).toHaveTextContent(t("mail.help"));
      expect(block.queryByTestId("guide-fail-block")).toBeNull(); // its errors are the ones MailCard already shows
      expect(screen.getAllByText(t("mail.help"))).toHaveLength(1);
    });
  }
});

describe("the guide of Schwab names what the student sees on the site", () => {
  it("has Create App, Callback URL with the address, both products, Order Limit 120, Ready For Use and View Details", async () => {
    await show().catch(() => undefined);
    await pick("Schwab");
    const text = guide().textContent ?? "";
    for (const needle of [
      '"Create App"', '"Callback URL"', "https://127.0.0.1:8182", "Market Data Production", "Accounts and Trading Production",
      '"Order Limit"', "120", '"Ready For Use"', '"View Details"', '"Request Access"', '"Individual Developer"', "developer.schwab.com",
    ]) {
      expect(text, needle).toContain(needle);
    }
  });

  it("SiomTrading Futures never shows the text that still waits for SiomTrading", async () => {
    await show().catch(() => undefined);
    await pick("SiomTrading Futures");
    expect(document.body.textContent).not.toMatch(/POR CONFIRMAR/);
    const catalogs = [getCatalog("es"), getCatalog("en")];
    for (const catalog of catalogs) for (const [key, text] of Object.entries(catalog)) expect(text, key).not.toMatch(/POR CONFIRMAR/);
  });

  it("every guide key has the same parts in both languages", () => {
    const es = Object.keys(getCatalog("es")).filter((key) => key.startsWith("connections.guide."));
    const en = Object.keys(getCatalog("en")).filter((key) => key.startsWith("connections.guide."));
    expect(en.sort()).toEqual(es.sort());
    expect(es.length).toBeGreaterThan(30);
  });
});

describe("where the guide is opened from", () => {
  it("the fold opens the first time and can be folded and opened again", async () => {
    await show().catch(() => undefined);
    await pick("Alpaca IEX");
    const fold = screen.getByRole("button", { name: t("connections.guide.title") });
    expect(fold).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(fold);
    expect(fold).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByTestId("guide-what")).not.toBeVisible();
    await userEvent.click(fold);
    expect(screen.getByTestId("guide-what")).toBeVisible();
  });

  it("View guide of a row opens it in the dialog with no fields", async () => {
    Object.assign(backend.find("finnhub"), { added: true, key_state: "saved", state: "connected" });
    await show().catch(() => undefined);
    await userEvent.click(await screen.findByRole("button", { name: t("connections.row.menu", { name: "Finnhub" }) }));
    await userEvent.click(screen.getByRole("menuitem", { name: t("connections.row.guide") }));
    const open = await screen.findByRole("dialog");
    expect(within(open).getByTestId("guide-steps")).toBeVisible();
    expect(open.querySelectorAll("input")).toHaveLength(0);
    expect(within(open).queryByRole("button", { name: t("connections.add.testAndAdd") })).toBeNull();
  });

  it("View guide of the Gmail row shows mail.help", async () => {
    backend.mail = { ...backend.mail, connected: true, email: "ana@example.test" };
    await show().catch(() => undefined);
    await userEvent.click(await screen.findByRole("button", { name: t("connections.row.menu", { name: t("mail.title") }) }));
    await userEvent.click(screen.getByRole("menuitem", { name: t("connections.row.guide") }));
    expect(await screen.findByText(t("mail.help"))).toBeInTheDocument();
  });

  it("If the test fails appears under the error of Test and add, as well as in the guide", async () => {
    backend.failAdd.set("alpaca", BAD_KEY_TEST);
    await show().catch(() => undefined);
    await pick("Alpaca IEX");
    expect(screen.queryByTestId("guide-fail")).toBeNull(); // only the one inside the guide block before the error
    await userEvent.type(screen.getByLabelText(t("connections.field.keyId")), "AKID-1234567890");
    await userEvent.type(screen.getByLabelText(t("connections.field.secret")), SECRET);
    await userEvent.click(screen.getByRole("button", { name: t("connections.add.testAndAdd") }));
    const error = await screen.findByTestId("add-error");
    const failure = error.parentElement!;
    expect(within(failure).getByTestId("guide-fail")).toHaveTextContent(t("connections.guide.alpaca.fail"));
    expect(within(failure).getByTestId("guide-fail")).toHaveTextContent(t("connections.guide.fail"));
  });
});
