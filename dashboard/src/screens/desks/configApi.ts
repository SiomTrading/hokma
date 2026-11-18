import { api, ApiError } from "@/api";
import type { DeskConnection, DeskDetail, DeskPatch } from "./configTypes";

const base = (id: number) => `/api/desks/${id}`;

interface RawDetail extends Partial<Omit<DeskDetail, "assets">> {
  assets?: Array<string | { symbol: string }>;
  desk?: RawDetail;
}

/** Detail answers can carry the desk at the top or under `desk`, and assets as strings or as `{ symbol }`. */
function readDetail(body: RawDetail, id: number): DeskDetail {
  const src = body.desk ?? body;
  return {
    id: src.id ?? id,
    name: src.name ?? "",
    focus: src.focus ?? null,
    capital: src.capital ?? 0,
    objective_pct: src.objective_pct ?? null,
    mandate: src.mandate ?? null,
    token_budget: src.token_budget ?? null,
    assets: (src.assets ?? []).map((item) => (typeof item === "string" ? item : item.symbol)),
  };
}

interface RawConnection {
  provider_id?: string;
  id?: string;
  name?: string;
  enabled?: boolean;
  asset_classes?: string[];
}

function readConnection(raw: RawConnection): DeskConnection {
  const id = raw.provider_id ?? raw.id ?? "";
  return { provider_id: id, name: raw.name ?? id, enabled: raw.enabled === true, asset_classes: raw.asset_classes ?? [] };
}

/** Settings of one desk: its fields, its assets and the connections it uses. */
export const deskConfigApi = {
  detail: async (id: number): Promise<DeskDetail> => readDetail(await api.get<RawDetail>(base(id)), id),
  update: (id: number, patch: DeskPatch) => api.patch<unknown>(base(id), patch),
  /** Replaces the whole list; the server checks each symbol against the market catalog and answers 4xx with its reason. */
  setAssets: (id: number, assets: string[]) => api.put<unknown>(`${base(id)}/assets`, { assets }),
  /** Saves the order of the assets the desk already has; the server keeps their notes and refuses a different set. */
  reorderAssets: (id: number, assets: string[]) => api.put<unknown>(`${base(id)}/assets/order`, { assets }),
  connections: async (id: number): Promise<DeskConnection[]> => {
    const body = await api.get<RawConnection[] | { connections: RawConnection[] }>(`${base(id)}/connections`);
    return (Array.isArray(body) ? body : (body.connections ?? [])).map(readConnection);
  },
  setConnection: (id: number, providerId: string, enabled: boolean) =>
    api.put<unknown>(`${base(id)}/connections/${encodeURIComponent(providerId)}`, { enabled }),
};

/** Text a person can read for a failed call: the server's reason when it sent one, the fallback otherwise. */
export function failText(cause: unknown, fallback: string): string {
  return cause instanceof ApiError && cause.message ? cause.message : fallback;
}
