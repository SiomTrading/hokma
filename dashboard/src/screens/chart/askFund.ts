import { api } from "@/api";
import { getHub } from "@/screens/chat/hub";
import type { ChatHub } from "@/screens/chat/hub";

interface Uploaded {
  id: string;
  kind: string;
}

export class AskFundError extends Error {
  readonly code: string;
  constructor(code: string) {
    super(code);
    this.name = "AskFundError";
    this.code = code;
  }
}

const CONNECT_TIMEOUT_MS = 8000;
const CONVERSATION_TIMEOUT_MS = 15000;

function whenOpen(hub: ChatHub): Promise<void> {
  if (hub.status === "open") return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      off();
      reject(new AskFundError("offline"));
    }, CONNECT_TIMEOUT_MS);
    const off = hub.onStatus((status) => {
      if (status !== "open") return;
      clearTimeout(timer);
      off();
      resolve();
    });
  });
}

/**
 * Uploads the chart image (POST /api/attachments) and sends it in a NEW conversation over the chat socket
 * (`send` with `conversation_id: null`). Resolves with the id of the conversation the server created.
 */
function newRef(): string {
  return `ask-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export async function sendChartToChat(image: Blob, fileName: string, text: string, hub: ChatHub = getHub()): Promise<string> {
  const form = new FormData();
  form.append("file", image, fileName);
  const uploaded = await api.post<Uploaded>("/api/attachments", form, { timeoutMs: 60_000 });

  const ref = newRef();
  let stop: () => void = () => undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const created = new Promise<string>((resolve, reject) => {
    timer = setTimeout(() => reject(new AskFundError("timeout")), CONVERSATION_TIMEOUT_MS);
    stop = hub.watch((event) => {
      if (event.type === "conversation" && event.created && (event.client_ref === undefined || event.client_ref === ref)) resolve(event.conversation_id);
      else if (event.type === "error" && !event.conversation_id && (event.client_ref === undefined || event.client_ref === ref)) reject(new AskFundError(event.code));
    });
  });
  created.catch(() => undefined); // the failure is reported by the await below; this only avoids an unhandled rejection
  try {
    await whenOpen(hub);
    if (!hub.send({ type: "send", conversation_id: null, client_ref: ref, text, attachments: [uploaded.id] })) throw new AskFundError("offline");
    return await created;
  } finally {
    clearTimeout(timer);
    stop();
  }
}
