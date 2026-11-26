import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { getCatalog, setLocale, t } from "@/i18n";
import { installFakeSockets } from "@/test/fakeSocket";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { SchwabSampleDialog } from "./SchwabSample";

const ANSWER = { ok: true, file: "/home/user/Downloads/alpaca-account-sample-20261125T150000Z.json", folder_is_downloads: true, accounts: 1, positions: 3, activity: false };

beforeEach(() => {
  clearToken();
  setToken(TEST_TOKEN);
  setLocale("es");
  installFakeSockets();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("the anonymous sample of the Alpaca account (SPEC-026 3390e1b)", () => {
  it("says what it saves for Alpaca, asks the engine once for Alpaca (read only) and shows where the file is, with no message about the account stream", async () => {
    const fetchMock = vi.fn(async () => json(ANSWER));
    vi.stubGlobal("fetch", fetchMock);
    renderWithApp(<SchwabSampleDialog provider="alpaca" onClose={() => undefined} />);
    expect(screen.getByTestId("alpaca-sample")).toBeInTheDocument();
    expect(screen.getByText(t("connections.alpaca.sample.what"))).toHaveTextContent("XXXX");
    expect(screen.getByText(t("connections.alpaca.sample.what"))).toHaveTextContent("paper");
    await userEvent.click(screen.getByTestId("schwab-sample-run"));
    expect(await screen.findByTestId("schwab-sample-file")).toHaveTextContent(ANSWER.file);
    const result = screen.getByTestId("schwab-sample-result");
    expect(result).toHaveTextContent("3 posiciones en la muestra");
    expect(screen.queryByTestId("schwab-sample-activity")).not.toBeInTheDocument();
    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit | undefined]>;
    expect(calls).toHaveLength(1);
    expect(calls[0][0]).toBe("/api/alpaca/account-sample");
    expect((calls[0][1]?.method ?? "GET").toUpperCase()).toBe("POST");
  });

  it("shows the sentence of the engine when there is no Alpaca account yet and lets the owner try again", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ detail: { code: "alpaca_not_connected", message: "No Alpaca account is connected yet" } }, 409)));
    renderWithApp(<SchwabSampleDialog provider="alpaca" onClose={() => undefined} />);
    await userEvent.click(screen.getByTestId("schwab-sample-run"));
    expect(await screen.findByTestId("schwab-sample-error")).toBeInTheDocument();
    expect(screen.getByTestId("schwab-sample-run")).toHaveTextContent(t("connections.schwab.sample.again"));
  });

  it("is in both languages", () => {
    for (const key of ["connections.alpaca.sample", "connections.alpaca.sample.what", "connections.alpaca.sample.running", "connections.alpaca.sample.counts"]) {
      expect(getCatalog("en")[key]).toBeTruthy();
      expect(getCatalog("es")[key]).not.toBe(getCatalog("en")[key]);
    }
  });
});
