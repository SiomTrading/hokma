import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { installFakeSockets } from "@/test/fakeSocket";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { SchwabDiagnosticDialog } from "./SchwabDiagnostic";

const ANSWER = {
  ok: true,
  file: "/data/diagnostics/schwab-streaming-20261121T150000Z.json",
  messages: 321,
  summary: {
    heartbeat_interval_ms: { samples: 5, median: 10000, min: 9990, max: 10010 },
    chart_equity: { minutes_seen: 2, mode: "in_progress" },
    futures_data: {},
    status_codes: [],
  },
};

beforeEach(() => {
  clearToken();
  setToken(TEST_TOKEN);
  setLocale("en");
  installFakeSockets();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("the Schwab data check", () => {
  it("asks for one read only run, shows where the file went and says what to send", async () => {
    const fetchMock = vi.fn(async () => json(ANSWER));
    vi.stubGlobal("fetch", fetchMock);
    renderWithApp(<SchwabDiagnosticDialog onClose={() => undefined} />);
    expect(screen.getByText(t("connections.schwab.diagnostic.safe"))).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("schwab-diagnostic-run"));
    expect(await screen.findByTestId("schwab-diagnostic-file")).toHaveTextContent(ANSWER.file);
    expect(screen.getByTestId("schwab-diagnostic-result")).toHaveTextContent("10000 ms");
    expect(screen.getByTestId("schwab-diagnostic-result")).toHaveTextContent("in_progress");
    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit | undefined]>;
    expect(calls).toHaveLength(1);
    expect(calls[0][0]).toContain("/api/schwab/diagnostic/streaming");
    expect(calls[0][0]).not.toContain("token");                                // the token goes in the header, never in the address
    expect((calls[0][1]?.method ?? "GET").toUpperCase()).toBe("POST");
    expect(JSON.parse(String(calls[0][1]?.body))).toEqual({ symbol: "QQQ", seconds: 75 });
  });

  it("shows the reason when Schwab refuses and lets the student run it again", async () => {
    const fetchMock = vi.fn(async () => json({ detail: { code: "schwab.err.not_connected", message: "no session" } }, 409));
    vi.stubGlobal("fetch", fetchMock);
    renderWithApp(<SchwabDiagnosticDialog onClose={() => undefined} />);
    await userEvent.click(screen.getByTestId("schwab-diagnostic-run"));
    expect(await screen.findByTestId("schwab-diagnostic-error")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("schwab-diagnostic-run")).toHaveTextContent(t("connections.schwab.diagnostic.again")));
  });
});
