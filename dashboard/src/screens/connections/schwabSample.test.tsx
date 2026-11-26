import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { installFakeSockets } from "@/test/fakeSocket";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { SchwabSampleDialog } from "./SchwabSample";

const ANSWER = { ok: true, file: "/home/user/Downloads/schwab-account-sample-20261125T150000Z.json", folder_is_downloads: true, accounts: 2, positions: 3, activity: false };

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

describe("the anonymous sample of the account", () => {
  it("says what it saves, asks the engine once (read only) and shows where the file is and what to do with it", async () => {
    const fetchMock = vi.fn(async () => json(ANSWER));
    vi.stubGlobal("fetch", fetchMock);
    renderWithApp(<SchwabSampleDialog onClose={() => undefined} />);
    expect(screen.getByText(t("connections.schwab.sample.what"))).toHaveTextContent("XXXX");
    expect(screen.getByText(t("connections.schwab.sample.safe"))).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("schwab-sample-run"));
    expect(await screen.findByTestId("schwab-sample-file")).toHaveTextContent(ANSWER.file);
    const result = screen.getByTestId("schwab-sample-result");
    expect(result).toHaveTextContent(t("connections.schwab.sample.saved"));
    expect(result).toHaveTextContent("2 cuentas y 3 posiciones");
    expect(screen.getByTestId("schwab-sample-activity")).toHaveTextContent(t("connections.schwab.sample.activityNo"));
    expect(result).toHaveTextContent(t("connections.schwab.sample.send"));
    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit | undefined]>;
    expect(calls).toHaveLength(1);
    expect(calls[0][0]).toBe("/api/schwab/diagnostic/account-sample");
    expect((calls[0][1]?.method ?? "GET").toUpperCase()).toBe("POST");
    expect(calls[0][0]).not.toContain("token");
  });

  it("says where the file is when there is no downloads folder", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ ...ANSWER, folder_is_downloads: false, activity: true })));
    renderWithApp(<SchwabSampleDialog onClose={() => undefined} />);
    await userEvent.click(screen.getByTestId("schwab-sample-run"));
    expect(await screen.findByTestId("schwab-sample-result")).toHaveTextContent(t("connections.schwab.sample.savedElsewhere"));
    expect(screen.getByTestId("schwab-sample-activity")).toHaveTextContent(t("connections.schwab.sample.activityYes"));
  });

  it("shows the reason when Schwab refuses and lets the owner try again", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ detail: { code: "schwab.err.not_connected", message: "no session" } }, 409)));
    renderWithApp(<SchwabSampleDialog onClose={() => undefined} />);
    await userEvent.click(screen.getByTestId("schwab-sample-run"));
    expect(await screen.findByTestId("schwab-sample-error")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("schwab-sample-run")).toHaveTextContent(t("connections.schwab.sample.again")));
  });
});
