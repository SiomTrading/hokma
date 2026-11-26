import { useState, useSyncExternalStore } from "react";
import { useI18n } from "@/i18n";
import { usePanelState } from "@/shell/panelState";
import { normalizePath, resultFiles } from "../chat/activity";
import { useActivity } from "../chat/useActivity";
import { getSnapshot, openPreview, subscribe } from "../projects/store";
import { useAgentNamer } from "./agentNames";
import "./panel.css";

const MAX_FILES = 50;

export function useResultsSummary(): string {
  const { t } = useI18n();
  const { followed } = usePanelState();
  const n = resultFiles(useActivity(followed?.conversationId ?? null)).length;
  return t(n === 1 ? "panel.results.summary.one" : "panel.results.summary", { n });
}

/** Where a file sits in the projects of Hokma: the project and the path inside it, or null when it is outside all of them. */
function locate(list: ReturnType<typeof getSnapshot>["list"], path: string): { projectId: number; path: string; folder: string } | null {
  if (!list) return null;
  const clean = normalizePath(path);
  const absolute = clean.startsWith("/") || /^[A-Za-z]:\//.test(clean);
  const roots = list.projects.map((p) => ({ id: p.id, root: normalizePath(p.path).replace(/\/$/, "") }));
  for (const { id, root } of roots) {
    if (absolute && clean.startsWith(`${root}/`)) {
      const rest = clean.slice(root.length + 1);
      return { projectId: id, path: rest, folder: rest.includes("/") ? rest.slice(0, rest.lastIndexOf("/")) : "" };
    }
  }
  if (!absolute && list.active_id !== null) return { projectId: list.active_id, path: clean, folder: clean.includes("/") ? clean.slice(0, clean.lastIndexOf("/")) : "" };
  return null;
}

export function ResultsSection() {
  const { t } = useI18n();
  const { followed } = usePanelState();
  const conv = useActivity(followed?.conversationId ?? null);
  const namer = useAgentNamer();
  const { list } = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const [all, setAll] = useState(false);
  if (!followed) return <p className="hint pn-pad">{t("panel.results.none")}</p>;
  const files = resultFiles(conv);
  if (files.length === 0) return <p className="hint pn-pad">{t("panel.results.empty")}</p>;
  const shown = all ? files : files.slice(0, MAX_FILES);
  return (
    <div className="pn-list">
      {shown.map((file) => {
        const place = locate(list, file.path);
        const name = (place?.path ?? file.path).split("/").pop() ?? file.path;
        const body = (
          <>
            <span className="pn-main">
              <span className="pn-line">
                <span className="pn-name mono" title={file.path}>{name}</span>
                <span className="pn-clock">{file.created ? t("panel.results.created") : t("panel.results.modified")}</span>
              </span>
              <span className="pn-task hint">{place ? place.folder : file.path}</span>
              <span className="pn-task hint">{file.by ? namer(file.by) : t("panel.agents.main")}</span>
            </span>
          </>
        );
        return place ? (
          <button key={file.path} type="button" className="pn-row pn-click" data-testid="result-row" onClick={() => openPreview({ projectId: place.projectId, path: place.path, size: 0 })}>
            {body}
          </button>
        ) : (
          <div key={file.path} className="pn-row" data-testid="result-row">
            {body}
          </div>
        );
      })}
      {!all && files.length > MAX_FILES ? (
        <button type="button" className="chat-link pn-pad" onClick={() => setAll(true)}>
          {t("panel.agents.more", { n: files.length - MAX_FILES })}
        </button>
      ) : null}
    </div>
  );
}
