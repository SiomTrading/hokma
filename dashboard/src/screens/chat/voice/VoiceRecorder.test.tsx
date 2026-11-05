import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { VoiceRecorder, formatClock } from "./VoiceRecorder";
import type { TranscribeResult, VoiceStatus } from "./voiceApi";

/** The real client and component run; only the network and the browser recorder are replaced. */
interface Reply {
  status?: number;
  body: unknown;
}

const OFFLINE = "No internet: connect to download the model.";

const statusReplies: Reply[] = [];
const modelReplies: Reply[] = [];
const transcribeReplies: Reply[] = [];
const transcribeCalls: Array<{ url: string; body: Blob }> = [];

function status(patch: Partial<VoiceStatus> = {}): Reply {
  return {
    body: { available: true, model: "small", state: "ready", progress: 1, bytes_done: 0, bytes_total: 0, download_mb: 485, error: null, ...patch },
  };
}

function result(text: string, id = "7"): Reply {
  const body: TranscribeResult = { text, attachment_id: id, name: "n.webm", mime: "audio/webm", size: 4, language: "es", duration: 2 };
  return { status: 201, body };
}

function failure(code: string, message: string, httpStatus: number): Reply {
  return { status: httpStatus, body: { detail: { code, message } } };
}

/** The last reply repeats, so a polling loop keeps getting the same answer. */
function next(queue: Reply[]): Reply {
  return queue.length > 1 ? (queue.shift() as Reply) : queue[0];
}

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = String(input);
  let reply: Reply;
  if (url.startsWith("/api/transcribe/status")) reply = next(statusReplies);
  else if (url.startsWith("/api/transcribe/model")) reply = next(modelReplies);
  else {
    transcribeCalls.push({ url, body: init?.body as Blob });
    reply = next(transcribeReplies);
  }
  return Promise.resolve(new Response(JSON.stringify(reply.body), { status: reply.status ?? 200 }));
}

class FakeRecorder {
  static instances: FakeRecorder[] = [];
  static isTypeSupported = (type: string) => type === "audio/webm;codecs=opus";
  state: "inactive" | "recording" = "inactive";
  mimeType = "audio/webm;codecs=opus";
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public stream: MediaStream, public options?: { mimeType?: string }) {
    FakeRecorder.instances.push(this);
  }
  start() {
    this.state = "recording";
  }
  stop() {
    this.state = "inactive";
    this.ondataavailable?.({ data: new Blob([new Uint8Array([1, 2, 3, 4])], { type: this.mimeType }) });
    this.onstop?.();
  }
}

const trackStop = vi.fn();
let getUserMedia: ReturnType<typeof vi.fn>;

beforeEach(() => {
  setToken("test-session-token-0123456789");
  statusReplies.length = modelReplies.length = transcribeReplies.length = transcribeCalls.length = 0;
  FakeRecorder.instances = [];
  trackStop.mockReset();
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
  vi.stubGlobal("MediaRecorder", FakeRecorder);
  getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [{ stop: trackStop }] });
  Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia }, configurable: true });
});

afterEach(() => vi.unstubAllGlobals());

function mount(overrides: Partial<Parameters<typeof VoiceRecorder>[0]> = {}) {
  const onTranscribed = vi.fn();
  const onError = vi.fn();
  const view = renderWithApp(<VoiceRecorder onTranscribed={onTranscribed} onError={onError} {...overrides} />);
  return { onTranscribed, onError, ...view };
}

const mic = () => screen.getByRole("button", { name: "Grabar nota de voz" });
const stopButton = () => screen.findByRole("button", { name: "Detener y transcribir" });

describe("VoiceRecorder", () => {
  it("records, shows the clock, transcribes and hands the text and the attachment id up", async () => {
    statusReplies.push(status());
    transcribeReplies.push(result("hola mundo"));
    const { onTranscribed, onError } = mount({ language: "es" });
    await userEvent.click(mic());
    expect(await screen.findByRole("timer")).toHaveTextContent("00:00");
    expect(screen.getByRole("button", { name: t("voice.cancel", undefined, "es") })).toBeInTheDocument();
    await userEvent.click(await stopButton());
    await waitFor(() => expect(onTranscribed).toHaveBeenCalledWith("hola mundo", "7"));
    expect(onError).not.toHaveBeenCalled();
    expect(transcribeCalls).toHaveLength(1);
    expect(transcribeCalls[0].url).toBe("/api/transcribe?language=es");
    expect(transcribeCalls[0].body.size).toBe(4);
    expect(FakeRecorder.instances[0].options?.mimeType).toBe("audio/webm;codecs=opus");
    expect(trackStop).toHaveBeenCalled();                     // the microphone is released
    expect(mic()).toBeEnabled();
  });

  it("cancel discards the recording and sends nothing", async () => {
    statusReplies.push(status());
    const { onTranscribed, onError } = mount();
    await userEvent.click(mic());
    await userEvent.click(await screen.findByRole("button", { name: t("voice.cancel", undefined, "es") }));
    expect(transcribeCalls).toHaveLength(0);
    expect(onTranscribed).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
    expect(trackStop).toHaveBeenCalled();
    expect(mic()).toBeInTheDocument();
  });

  it("asks before the one-time download, shows progress and then lets the owner record", async () => {
    statusReplies.push(
      status({ state: "missing", progress: 0 }),
      status({ state: "downloading", progress: 0.5, bytes_done: 243_000_000, bytes_total: 486_000_000 }),
      status(),
    );
    modelReplies.push(status({ state: "downloading", progress: 0 }));
    mount();
    await userEvent.click(mic());
    expect(await screen.findByText(/unos 485 MB/)).toBeInTheDocument();
    expect(screen.getByText(/nunca sale de tu computador/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Descargar" }));
    expect(await screen.findByText("Descargando el modelo de voz")).toBeInTheDocument();
    expect(await screen.findByText(/243 de 486 MB/, undefined, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
    expect(await screen.findByText(t("voice.ready", undefined, "es"), undefined, { timeout: 3000 })).toBeInTheDocument();
    expect(transcribeCalls).toHaveLength(0);
  });

  it("says clearly that there is no internet when the download fails, and can retry", async () => {
    statusReplies.push(
      status({ state: "missing" }),
      status({ state: "error", error: { code: "offline", message: OFFLINE } }),
    );
    modelReplies.push(status({ state: "downloading", progress: 0 }));
    mount();
    await userEvent.click(mic());
    await userEvent.click(await screen.findByRole("button", { name: "Descargar" }));
    expect(await screen.findByText(OFFLINE, undefined, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reintentar" })).toBeInTheDocument();
  });

  it("a model that disappears after recording keeps the audio and transcribes it when the download ends", async () => {
    statusReplies.push(status());
    transcribeReplies.push(failure("model_missing", "The voice model is not downloaded yet.", 409), result("texto", "9"));
    modelReplies.push(status({ state: "downloading", progress: 0.1 }));
    const { onTranscribed } = mount();
    await userEvent.click(mic());
    await userEvent.click(await stopButton());
    await waitFor(() => expect(onTranscribed).toHaveBeenCalledWith("texto", "9"), { timeout: 4000 });
    expect(transcribeCalls).toHaveLength(2);
    expect(transcribeCalls[1].body).toBe(transcribeCalls[0].body);      // the same recording, not a new one
  });

  it.each([
    ["NotAllowedError", t("voice.error.mic_denied", undefined, "es")],
    ["NotFoundError", t("voice.error.mic_missing", undefined, "es")],
  ])("explains a microphone failure (%s)", async (name, message) => {
    statusReplies.push(status());
    getUserMedia.mockRejectedValue(new DOMException("x", name));
    const { onError } = mount();
    await userEvent.click(mic());
    await waitFor(() => expect(onError).toHaveBeenCalledWith(message));
    expect(mic()).toBeEnabled();
  });

  it("reports silence and server errors through onError", async () => {
    statusReplies.push(status());
    transcribeReplies.push(result("  "), failure("transcribe_failed", "No se pudo transcribir el audio.", 500));
    const { onError } = mount();
    await userEvent.click(mic());
    await userEvent.click(await stopButton());
    await waitFor(() => expect(onError).toHaveBeenCalledWith(t("voice.error.no_speech", undefined, "es")));

    await userEvent.click(mic());
    await userEvent.click(await stopButton());
    await waitFor(() => expect(onError).toHaveBeenLastCalledWith("No se pudo transcribir el audio."));
  });

  it("says so when the speech engine is not installed", async () => {
    statusReplies.push(status({ available: false, state: "missing" }));
    const { onError } = mount();
    await userEvent.click(mic());
    await waitFor(() => expect(onError).toHaveBeenCalledWith(expect.stringContaining(t("voice.error.engine_missing", undefined, "es").slice(0, 8))));
  });

  it("is disabled while the chat is busy", () => {
    mount({ disabled: true });
    expect(mic()).toBeDisabled();
  });

  it("frees the microphone when it is removed during a recording", async () => {
    statusReplies.push(status());
    const { onTranscribed, unmount } = mount();
    await userEvent.click(mic());
    await screen.findByRole("timer");
    unmount();
    expect(FakeRecorder.instances[0].state).toBe("inactive");
    expect(trackStop).toHaveBeenCalled();
    expect(onTranscribed).not.toHaveBeenCalled();
    expect(transcribeCalls).toHaveLength(0);
  });

  it("formats the clock", () => {
    expect(formatClock(0)).toBe("00:00");
    expect(formatClock(65.9)).toBe("01:05");
    expect(formatClock(600)).toBe("10:00");
  });
});
