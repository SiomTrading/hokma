import { useState } from "react";
import { Button, Pane } from "@/design";
import { useI18n } from "@/i18n";
import { DocumentsSection } from "@/screens/projects/DocumentsSection";
import "./library.css";
import { LibraryFolders } from "./LibraryFolders";
import type { Crumb } from "./LibraryFolders";
import { ModelBand } from "./ModelBand";

/** The student Library: every document the agent can consult in any conversation, kept in folders. */
export function LibraryScreen() {
  const { t } = useI18n();
  const [trail, setTrail] = useState<Crumb[]>([]);
  const [creating, setCreating] = useState(false);
  const [version, setVersion] = useState(0);
  const folderId = trail.length > 0 ? trail[trail.length - 1]!.id : null;
  const bump = () => setVersion((n) => n + 1);

  return (
    <Pane as="section" aria-label={t("screen.library")} style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "auto" }}>
      <div className="lib-page" data-testid="library-screen">
        <h1 className="disp">{t("library.title")}</h1>
        <ModelBand />
        <LibraryFolders
          trail={trail}
          onNavigate={(next) => {
            setCreating(false);
            setTrail(next);
          }}
          creating={creating}
          onCloseCreate={() => setCreating(false)}
          version={version}
          onMoved={bump}
        />
        <DocumentsSection
          projectId="library"
          folderId={folderId}
          reloadKey={version}
          actions={
            <Button size="sm" onClick={() => setCreating(true)}>
              {t("library.folder.new")}
            </Button>
          }
        />
      </div>
    </Pane>
  );
}
