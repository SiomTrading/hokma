import { api } from "@/api";

export type ModelState = "downloading" | "ready" | "failed";

/** Answer of `GET /api/library/model`: the search model the app prepares by itself. */
export interface ModelStatus {
  state: ModelState;
  /** 0 to 100. */
  percent: number;
  bytes: number;
  total: number;
}

export const getModelStatus = (): Promise<ModelStatus> => api.get<ModelStatus>("/api/library/model");

/** Only accepted while the state is `failed`; the backend answers 409 otherwise. */
export const retryModel = (): Promise<ModelStatus> => api.post<ModelStatus>("/api/library/model/retry");
