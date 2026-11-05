import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { t } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { renderWithApp } from "@/test/utils";
import { ChatScreen } from "./ChatScreen";
import { setHub } from "./hub";
import { FakeServer } from "./testkit";
import { voice } from "./voiceTypes";
import type { VoiceRecorderProps } from "./voiceTypes";

function FakeRecorder({ onTranscribed, onError }: VoiceRecorderProps) {
  return (
    <>
      <button type="button" onClick={() => onTranscribed("save it and remind me", "77")}>
        record
      </button>
      <button type="button" onClick={() => onError()}>
        fail
      </button>
    </>
  );
}

function Toasts() {
  const shell = useShell();
  return (
    <ul>
      {shell.toasts.map((toast) => (
        <li key={toast.id}>{toast.text}</li>
      ))}
    </ul>
  );
}

const original = voice.Recorder;
beforeEach(() => {
  voice.Recorder = FakeRecorder;
});
afterEach(() => {
  voice.Recorder = original;
  setHub(null);
  vi.unstubAllGlobals();
});

describe("voice note in the composer", () => {
  it("puts the transcription in the message and keeps the audio as a transcribed attachment", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ agents: [], projects: [], default_project: { id: "default", name: "Main" } }))));
    const server = new FakeServer();
    renderWithApp(
      <>
        <ChatScreen tabKey="chat" params={{}} active />
        <Toasts />
      </>,
    );
    server.open();
    await userEvent.click(screen.getByRole("button", { name: "record" }));
    const box = screen.getByLabelText(t("chat.composer.label")) as HTMLTextAreaElement;
    expect(box.value).toBe("save it and remind me");
    expect(screen.getByTestId("pending-attachment")).toHaveTextContent(t("chat.voice.transcribed"));
    await userEvent.keyboard("{Enter}");
    expect(server.last("send")).toMatchObject({ text: "save it and remind me", attachments: ["77"] });
    expect(screen.getByTestId("user-message")).toHaveTextContent(t("chat.voice.transcribed"));
    await userEvent.click(screen.getByRole("button", { name: "fail" }));
    expect(await screen.findByText(t("chat.voice.error"))).toBeInTheDocument();
  });
});
