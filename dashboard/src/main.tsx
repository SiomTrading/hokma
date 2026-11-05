import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { t } from "./i18n";

function App() {
  return (
    <main className="min-h-screen flex flex-col items-center justify-center gap-4">
      <img src="/hokma-isotype.svg" alt="Hokma" className="w-24 h-24" />
      <h1 className="font-display text-5xl text-brand-mist">Hokma</h1>
      <p className="text-brand-teal">{t("tagline")}</p>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
