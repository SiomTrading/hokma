import { EmptyState, IconButton, Modal, NavItem } from "@/design";
import { useI18n } from "@/i18n";
import { getSettingsSection } from "./registry";
import { settingsSectionList } from "./screens";
import { Boundary } from "./Slot";
import { useShell } from "./store";

export function SettingsModal() {
  const { t } = useI18n();
  const shell = useShell();
  const sections = settingsSectionList();
  const current = sections.find((s) => s.id === shell.settings.section) ?? sections[0]!;
  const Body = getSettingsSection(current.id)?.component;

  return (
    <Modal label={t("settings.title")} onClose={shell.closeSettings}>
      <nav className="snav" aria-label={t("settings.sections")}>
        <span className="sec hide-c" style={{ paddingBottom: 8 }}>
          {t("settings.title")}
        </span>
        {sections.map((section) => (
          <NavItem
            key={section.id}
            icon={section.icon}
            iconSize={16}
            label={t(section.titleKey)}
            active={section.id === current.id}
            onClick={() => shell.openSettings(section.id)}
          />
        ))}
      </nav>
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "16px 20px", borderBottom: "1px solid var(--border)" }}>
          <h2 className="h1" style={{ fontSize: 17 }}>
            {t(current.titleKey)}
          </h2>
          <IconButton icon="close" size={16} label={t("settings.close")} onClick={shell.closeSettings} />
        </div>
        <div style={{ flex: 1, overflow: "auto", padding: "18px 20px" }} key={current.id}>
          <Boundary>
            {Body ? (
              <div className="fade">
                <Body />
              </div>
            ) : (
              <EmptyState title={t("settings.unavailable")} />
            )}
          </Boundary>
        </div>
      </div>
    </Modal>
  );
}
