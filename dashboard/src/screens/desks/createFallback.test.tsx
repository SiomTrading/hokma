import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { renderWithApp, TEST_TOKEN } from "@/test/utils";
import { CreateDeskModal } from "./CreateDeskModal";
import { resetDesksStore } from "./store";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function stub(connections: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = String(url);
      if (path === "/api/desks/templates") return json({ templates: [{ id: "standard", token_budget: 100000, agents: [{ slug: "a", role: "chief", model: "opus" }] }] });
      if (path === "/api/desks" && init?.method === "POST") return json({ id: 9, connections });
      if (path === "/api/desks") return json({ desks: [] });
      return json({});
    }),
  );
}

async function createDesk(onClose: () => void = () => undefined) {
  setToken(TEST_TOKEN);
  renderWithApp(<CreateDeskModal onClose={onClose} />);
  await userEvent.click(await screen.findByRole("button", { name: new RegExp(t("desks.start.template.name")) }));
  await userEvent.type(await screen.findByLabelText(t("desks.field.name")), "Mesa prueba");
  await userEvent.click(screen.getByRole("button", { name: t("desks.wizard.next.team") }));
  const create = await screen.findByRole("button", { name: t("desks.wizard.create") });
  await vi.waitFor(() => expect(create).toBeEnabled());
  await userEvent.click(create);
}

beforeEach(() => {
  setLocale("es");
  resetDesksStore();
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("creating a desk that only has the Yahoo fallback", () => {
  it("says so and offers the link to Connections", async () => {
    stub({ providers: [], fallback_only: true });
    await createDesk();
    expect(await screen.findByText(t("desks.create.fallback.text"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("desks.create.fallback.go") })).toBeInTheDocument();
  });

  it("shows no notice when the desk was born with providers", async () => {
    stub({ providers: ["schwab"], fallback_only: false });
    const onClose = vi.fn();
    await createDesk(onClose);
    await vi.waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(screen.queryByText(t("desks.create.fallback.text"))).not.toBeInTheDocument();
  });
});
