import { useEffect, useState } from "react";
import { Button, ErrorState, Loading, Modal } from "@/design";
import { useI18n } from "@/i18n";
import { loadFile, PREVIEW_TEXT_LIMIT } from "./api";
import type { FilePreview } from "./api";
import { messageOf } from "./store";

interface Props {
  projectId: number;
  path: string;
  size: number;
  onClose: () => void;
}

/** Everything the file holds is shown as text or as a media element; nothing from the file is ever HTML. */
export function PreviewDialog({ projectId, path, size, onClose }: Props) {
  const { t } = useI18n();
  const name = path.split("/").pop() ?? path;
  const [state, setState] = useState<{ file?: FilePreview; error?: string }>({});

  useEffect(() => {
    let url: string | undefined;
    let cancelled = false;
    setState({});
    loadFile(projectId, path).then(
      (file) => {
        url = file.url;
        if (cancelled && url) URL.revokeObjectURL(url);
        else if (!cancelled) setState({ file });
      },
      (cause: unknown) => !cancelled && setState({ error: messageOf(cause) }),
    );
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [projectId, path]);

  const file = state.file;
  return (
    <Modal label={t("projects.preview.title", { name })} onClose={onClose} style={{ width: "min(720px, 100%)", flexDirection: "column" }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: 22, minHeight: 0, width: "100%" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <h2 className="h2" style={{ wordBreak: "break-all" }}>
            {name}
          </h2>
          <Button size="sm" variant="ghost" onClick={onClose} style={{ marginLeft: "auto" }}>
            {t("projects.close")}
          </Button>
        </div>
        <div style={{ overflow: "auto", minHeight: 80 }}>
          {state.error ? <ErrorState message={state.error} /> : null}
          {!file && !state.error ? <Loading label={t("projects.preview.loading")} inline /> : null}
          {file?.text !== undefined ? (
            <>
              <pre className="mono" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 12, margin: 0 }}>
                {file.text}
              </pre>
              {size > PREVIEW_TEXT_LIMIT ? <p className="p">{t("projects.preview.truncated")}</p> : null}
            </>
          ) : null}
          {file?.url && file.mime.startsWith("image/") ? (
            <img src={file.url} alt={name} style={{ maxWidth: "100%", maxHeight: 420, borderRadius: 10 }} />
          ) : null}
          {file?.url && file.mime.startsWith("audio/") ? <audio controls src={file.url} style={{ width: "100%" }} /> : null}
          {file?.url && !file.mime.startsWith("image/") && !file.mime.startsWith("audio/") ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <p className="p">{t("projects.preview.none")}</p>
              <div style={{ display: "flex", gap: 8 }}>
                <a className="btn sm" href={file.url} download={name}>
                  {t("projects.preview.download")}
                </a>
                {file.mime === "application/pdf" ? (
                  <a className="btn sm ghost" href={file.url} target="_blank" rel="noopener noreferrer">
                    {t("projects.preview.openTab")}
                  </a>
                ) : null}
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </Modal>
  );
}
