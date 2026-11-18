import { useState } from "react";
import { useI18n } from "@/i18n";
import { setChatStart } from "@/screens/chat/start";
import { useShell } from "@/shell/useShell";
import { newDraftId } from "@/shell/tabsStorage";
import { StartModal } from "./StartModal";
import { refreshDesks } from "./store";
import { TemplateWizard } from "./TemplateWizard";

type Shell = ReturnType<typeof useShell>;
type Translate = ReturnType<typeof useI18n>["t"];

/**
 * "Create it with Hokma": opens a new chat with a first message that asks for the desk, and says so. Hokma
 * proposes the values in the chat and creates the desk once the owner approves its permission request.
 */
export function askHokmaForDesk(shell: Shell, t: Translate): void {
  const draft = newDraftId();
  setChatStart(draft, t("desks.hokma.start"));
  shell.openScreen("chat", { draft });
  shell.toast({ tone: "info", text: t("desks.hokma.toast") });
}

interface Props {
  onClose: () => void;
}

/** The whole "create a desk" flow: the two paths first, then the four-step assistant when the template path is chosen. */
export function CreateDeskModal({ onClose }: Props) {
  const { t } = useI18n();
  const shell = useShell();
  const [stage, setStage] = useState<"start" | "wizard">("start");

  const created = async (id: number) => {
    await refreshDesks();
    shell.toast({ tone: "ok", text: t("desks.create.done") });
    onClose();
    shell.openScreen("desks", { desk: String(id) });
  };

  if (stage === "wizard") return <TemplateWizard onClose={onClose} onCreated={(id) => void created(id)} />;
  return (
    <StartModal
      onClose={onClose}
      onHokma={() => {
        onClose();
        askHokmaForDesk(shell, t);
      }}
      onTemplate={() => setStage("wizard")}
    />
  );
}
