import { act, cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { FakeSocket, installFakeSockets } from "@/test/fakeSocket";
import { renderWithApp, TEST_TOKEN } from "@/test/utils";
import { AssetList } from "./AssetList";

const noop = () => undefined;

async function show(extra: Record<string, unknown>) {
  renderWithApp(<AssetList symbols={["NQ=F"]} focus="NQ=F" onFocus={noop} adding={false} busy={false} error={null} onToggleAdd={noop} onAdd={noop} onRemove={noop} onReorder={noop} />);
  await waitFor(() => expect(FakeSocket.of("/ws/market").length).toBeGreaterThan(0));
  const socket = FakeSocket.of("/ws/market")[0]!;
  act(() => socket.serverOpen());
  const sub = String(socket.ops("subscribe")[0]!.sub);
  act(() => socket.serverSend({ type: "snapshot", sub, seq: 1, epoch: "e1", last: 21430.25, previous_close: 21400, change_pct: 0.1, ...extra }));
}

beforeEach(() => {
  setLocale("es");
  clearToken();
  setToken(TEST_TOKEN);
  installFakeSockets();
});
afterEach(() => cleanup());

describe("the price of an asset says which provider served it", () => {
  it("names the provider of the price", async () => {
    await show({ source: "Schwab", data_state: "live" });
    expect(await screen.findByTestId("quote-source")).toHaveTextContent(t("desks.terminal.priceFrom", { source: "Schwab" }));
  });

  it("marks a delayed provider as delayed", async () => {
    await show({ source: "Yahoo Finance", data_state: "delayed" });
    expect(await screen.findByTestId("quote-source")).toHaveTextContent(t("desks.terminal.priceDelayed", { source: "Yahoo Finance" }));
  });

  it("says nothing when the message does not name a provider", async () => {
    await show({});
    await act(async () => undefined);
    expect(screen.queryByTestId("quote-source")).not.toBeInTheDocument();
  });
});
