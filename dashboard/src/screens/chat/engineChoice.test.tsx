import { renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { MessageItem } from "./Messages";
import { useEngines } from "../engines/useEngines";
import { errorText } from "./chatApi";
import { initialState, reducer } from "./state";
import type { Action } from "./state";
import type { ServerEvent } from "./types";

// Bug 1095: the saved engine was Gemini, Claude was the one connected, and the chat went to Gemini while the screen said Claude.

const ev = (event: Record<string, unknown>): Action => ({ type: "event", event: event as unknown as ServerEvent, errorText });

function listing(body: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ connections: [], detect: { cli: {}, codex_login: false, claude_import: false, providers: [] }, store_available: true, ...body }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    ),
  );
}

beforeEach(() => {
  clearToken();
  setToken(TEST_TOKEN);
  setLocale("es");
});
afterEach(() => vi.unstubAllGlobals());

describe("the engine the screen shows is the one that answers", () => {
  it("shows model names when the server says Claude answers, even if Gemini is the saved default", async () => {
    listing({
      connections: [{ id: 1, engine: "gemini_cli", label: "Gemini", status: "error", is_default: true }],
      active: { id: null, engine: "claude_code", label: "Claude", replaced: true, available: true, saved: "Gemini" },
    });
    const { result } = renderHook(() => useEngines("7"));
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalled());
    expect(String(vi.mocked(fetch).mock.calls[0]![0])).toContain("conversation_id=7");
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(result.current.active?.engine).toBe("claude_code");   // not the saved Gemini
  });

  it("shows levels when the server says another engine answers", async () => {
    listing({ active: { id: 2, engine: "codex", label: "ChatGPT", replaced: false, available: true, saved: null } });
    const { result } = renderHook(() => useEngines(null));
    await waitFor(() => expect(result.current.active?.engine).toBe("codex"));
  });
});

describe("the chat tells what happened", () => {
  it("shows a short notice when the engine was replaced for the turn", () => {
    const state = reducer(initialState("7"), ev({ type: "engine_notice", conversation_id: "7", message: "Gemini is not available on this computer. This conversation uses Claude." }));
    expect(state.items[0]).toMatchObject({ kind: "notice", tone: "warn" });
    expect((state.items[0] as { text: string }).text).toContain("uses Claude");
  });

  it("with no engine connected says it in plain words, without a technical detail", () => {
    const state = reducer(initialState("7"), ev({ type: "error", code: "no_engine", message: "no engine is installed and connected", conversation_id: "7" }));
    const text = (state.items[0] as { text: string }).text;
    expect(text).toBe(t("chat.err.no_engine"));
    expect(text).toContain("Ajustes > Motores");
    expect(text).not.toContain("no engine is installed");
  });
});

describe("a signed-out Claude and a blocked file in the chat", () => {
  it("auth leads to Engines; integrity_check has its own text, not the save-failed one (bugs 1112, 1113)", () => {
    const auth = reducer(initialState("7"), ev({ type: "error", code: "auth", message: "Not logged in", conversation_id: "7" }));
    expect(auth.items[0]).toMatchObject({ kind: "notice", action: "open_engines" });
    const blocked = reducer(initialState("7"), ev({ type: "error", code: "integrity_check", message: "IntegrityError: x", conversation_id: "7" }));
    const text = (blocked.items[0] as { text: string }).text;
    expect(text).toContain(t("chat.err.integrity_check"));
    expect(text).not.toContain(t("chat.err.integrity"));
  });
});

describe("the no engine message leads to Settings > Engines", () => {
  it("the reducer marks it with an action and the message shows a button that opens the engines section", async () => {
    const state = reducer(initialState("7"), ev({ type: "error", code: "no_engine", message: "x", conversation_id: "7" }));
    const item = state.items[0]!;
    expect(item).toMatchObject({ kind: "notice", action: "open_engines" });
    function Probe() {
      const shell = useShell();
      return <span data-testid="section">{shell.settings.open ? shell.settings.section : "closed"}</span>;
    }
    const actions = { projectName: "", canSave: false, onPermission: () => undefined, onSaveMessage: () => undefined, onSaveAttachment: () => undefined };
    renderWithApp(
      <>
        <MessageItem item={item} actions={actions} />
        <Probe />
      </>,
    );
    expect(screen.getByTestId("section")).toHaveTextContent("closed");
    await userEvent.setup().click(screen.getByRole("button", { name: t("chat.err.openEngines") }));
    expect(screen.getByTestId("section")).toHaveTextContent("engines");
  });

  it("other errors have no button", () => {
    const state = reducer(initialState("7"), ev({ type: "error", code: "engine_error", message: "x", conversation_id: "7" }));
    expect(state.items[0]).not.toHaveProperty("action");
  });
});
