import { api } from "@/api";
import type { AgentItem, JobInput, JobItem, ModelAlias, NewAgent, UsageData } from "./types";

export const agentsApi = {
  setModel: (slug: string, model: ModelAlias) => api.put<AgentItem>(`/api/agents/${encodeURIComponent(slug)}/model`, { model }),
  setStatus: (slug: string, status: "active" | "off") =>
    api.put<AgentItem>(`/api/agents/${encodeURIComponent(slug)}/status`, { status }),
  create: (agent: NewAgent) => api.post<AgentItem>("/api/agents", agent),
};

export const usageApi = {
  weekly: () => api.get<UsageData>("/api/usage/weekly"),
  setCap: (cap: number | null) => api.put<UsageData>("/api/usage/cap", { cap }),
};

export const jobsApi = {
  create: (job: JobInput) => api.post<JobItem>("/api/scheduled-jobs", job),
  update: (id: number, job: JobInput) => api.put<JobItem>(`/api/scheduled-jobs/${id}`, job),
  remove: (id: number) => api.delete<void>(`/api/scheduled-jobs/${id}`),
  activate: (id: number) => api.post<JobItem>(`/api/scheduled-jobs/${id}/activate`, { confirm: true }),
  deactivate: (id: number) => api.post<JobItem>(`/api/scheduled-jobs/${id}/deactivate`),
};
