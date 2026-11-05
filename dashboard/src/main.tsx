import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { initToken } from "@/api";
import "@/shell/autoload";
import { getLocale } from "@/i18n";
import { App } from "./App";

initToken();
document.documentElement.lang = getLocale();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
