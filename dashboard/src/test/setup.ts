import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";
import "@/shell/autoload";
import { setLocale } from "@/i18n";

beforeEach(() => {
  localStorage.clear();
  setLocale("es");
});

afterEach(() => {
  cleanup();
});
