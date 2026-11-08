import { useState } from "react";
import { Button, Modal } from "@/design";
import { useI18n } from "@/i18n";
import { NOTICES } from "./notices";

/** "Open source notices": a link that opens the notice of every library whose license asks for it. */
export function OpenSourceNotices() {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);

  return (
    <>
      <Button size="sm" variant="ghost" onClick={() => setOpen(true)} data-testid="open-source-notices" style={{ alignSelf: "flex-start" }}>
        {t("sys.notices")}
      </Button>
      {open ? (
        <Modal label={t("sys.notices.title")} onClose={() => setOpen(false)} style={{ width: "min(560px, 100%)", flexDirection: "column" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "16px 20px", borderBottom: "1px solid var(--border)" }}>
            <span className="h2" style={{ flex: 1 }}>{t("sys.notices.title")}</span>
            <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>{t("sys.notices.close")}</Button>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 16, padding: "16px 20px", overflow: "auto" }}>
            <p className="p" style={{ fontSize: 12, margin: 0 }}>{t("sys.notices.intro")}</p>
            {NOTICES.map((item) => (
              <section key={item.name} aria-label={item.name} data-testid={`notice-${item.name}`} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <span className="mono" style={{ fontSize: 13, color: "var(--t1)" }}>{item.name}</span>
                <span className="p" style={{ fontSize: 12 }}>
                  {t("sys.notices.license", { license: item.license })} ·{" "}
                  <a href={item.licenseUrl} target="_blank" rel="noreferrer noopener" style={{ color: "var(--acc)" }}>
                    {t("sys.notices.readLicense")}
                  </a>
                </span>
                <pre className="mono" data-testid="notice-text" style={{ margin: 0, padding: "10px 12px", fontSize: 11, color: "var(--t2)", background: "var(--glass-subtle)", border: "1px solid var(--border)", borderRadius: 10, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                  {item.notice}
                </pre>
              </section>
            ))}
          </div>
        </Modal>
      ) : null}
    </>
  );
}
