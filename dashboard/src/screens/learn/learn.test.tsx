import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "@/i18n";
import { failure, mockApi } from "@/screens/system/testkit";
import { formatShortcut, SHORTCUTS } from "@/shell/shortcuts";
import { t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { HomeScreen } from "@/screens/home/HomeScreen";
import { LearnScreen } from "./LearnScreen";
import { Markdown } from "./Markdown";

afterEach(() => vi.unstubAllGlobals());

type Loc = "es" | "en";
function learnWords(l: Loc) {
  return {
    search: t("learn.search.label", undefined, l),
    first: t("learn.firstDay.title", undefined, l),
    quick: t("learn.commands.title", undefined, l),
    short: t("learn.shortcuts.title", undefined, l),
    progress: t("learn.firstDay.progress", { done: 1, total: 3 }, l),
    none: t("learn.search.none", undefined, l),
    back: t("learn.back", undefined, l),
  };
}
function homeWords(l: Loc) {
  const greeting = ["morning", "afternoon", "evening"].map((d) => `^${t(`home.greeting.${d}`, { name: "Ana" }, l)}$`).join("|");
  return {
    greeting: new RegExp(greeting),
    talk: t("home.talk", undefined, l),
    agents: t("home.agents", undefined, l),
    cap: t("home.cap", undefined, l),
    fund: t("home.fund", { fund: "Fondo Uno" }, l),
  };
}

const PROPS = { tabKey: "learn", params: {}, active: true };
const TOPICS = [
  { slug: "first-steps", title: "First steps" },
  { slug: "extensions", title: "Extensions" },
];
const PROGRESS = (done: number) => ({
  ready: true,
  done,
  total: 3,
  steps: { first_conversation: done >= 1, first_project: done >= 2, first_image: done >= 3 },
});

describe.each([
  ["es", learnWords("es")],
  ["en", learnWords("en")],
] as const)("Learn (%s)", (locale, words) => {
  it("shows the three cards with real progress, the commands and every shortcut", async () => {
    setLocale(locale);
    mockApi({ "GET /api/learn/topics": TOPICS, "GET /api/learn/progress": PROGRESS(1) });
    renderWithApp(<LearnScreen {...PROPS} />);

    expect(await screen.findByText(words.first)).toBeInTheDocument();
    expect(screen.getByText(words.progress)).toBeInTheDocument();
    expect(screen.getByText(words.quick)).toBeInTheDocument();
    for (const command of ["hokma start", "hokma doctor", "hokma update", "hokma owner"]) {
      expect(screen.getByText(new RegExp(command))).toBeInTheDocument();
    }
    const card = screen.getByRole("region", { name: words.short });
    for (const shortcut of SHORTCUTS) {
      expect(within(card).getByText(formatShortcut(shortcut))).toBeInTheDocument();
      expect(within(card).getByText(t(shortcut.labelKey, undefined, locale))).toBeInTheDocument();
    }
    for (const slash of ["/modelo", "/agente", "/proyecto"]) expect(within(card).getByText(slash)).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: words.search })).toBeInTheDocument();
  });

  it("searches every file of the guide and opens a topic", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/learn/topics": TOPICS,
      "GET /api/learn/progress": PROGRESS(0),
      "GET /api/learn/search": [{ slug: "extensions", title: "Extensions", snippet: "Add a marketplace by link", score: 4 }],
      "GET /api/learn/topics/extensions": { slug: "extensions", title: "Extensions", body: "# Extensions\n\nTurn a plugin **on** with its switch.\n\n<script>alert(1)</script>" },
    });
    renderWithApp(<LearnScreen {...PROPS} />);
    await userEvent.type(await screen.findByRole("searchbox", { name: words.search }), "marketplace");
    const hit = await screen.findByText("Add a marketplace by link");
    expect(calls.find((c) => c.path.startsWith("/api/learn/search"))?.path).toContain(`q=marketplace&lang=${locale}`);

    await userEvent.click(hit);
    expect(await screen.findByRole("heading", { name: "Extensions" })).toBeInTheDocument();
    expect(screen.getByText("on").tagName).toBe("STRONG");
    expect(document.querySelector("script")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: words.back }));
    expect(await screen.findByText(words.first)).toBeInTheDocument();
  });

  it("says so when the search finds nothing", async () => {
    setLocale(locale);
    mockApi({ "GET /api/learn/topics": TOPICS, "GET /api/learn/progress": PROGRESS(0), "GET /api/learn/search": [] });
    renderWithApp(<LearnScreen {...PROPS} />);
    await userEvent.type(await screen.findByRole("searchbox", { name: words.search }), "zzzz");
    expect(await screen.findByText(words.none)).toBeInTheDocument();
  });
});

describe("Learn topics and errors", () => {
  it("opens a topic from the list of the guide", async () => {
    mockApi({
      "GET /api/learn/topics": TOPICS,
      "GET /api/learn/progress": PROGRESS(0),
      "GET /api/learn/topics/first-steps": { slug: "first-steps", title: "First steps", body: "# First steps\n\n1. Talk" },
    });
    renderWithApp(<LearnScreen {...PROPS} />);
    const list = await screen.findByRole("region", { name: t("learn.topics", undefined, "es") });
    await userEvent.click(await within(list).findByRole("button", { name: /First steps/ }));
    expect(await screen.findByRole("heading", { name: "First steps" })).toBeInTheDocument();
  });

  it("shows the error with retry when a topic cannot be read", async () => {
    mockApi({ "GET /api/learn/topics": TOPICS, "GET /api/learn/progress": PROGRESS(0), "GET /api/learn/topics/first-steps": failure(404, "That guide topic does not exist.") });
    renderWithApp(<LearnScreen {...PROPS} />);
    await userEvent.click(await screen.findByRole("button", { name: t("learn.firstDay.open", undefined, "es") }));
    expect(await screen.findByText("That guide topic does not exist.")).toBeInTheDocument();
  });

  it("marks the three steps done at 3 of 3", async () => {
    mockApi({ "GET /api/learn/topics": TOPICS, "GET /api/learn/progress": PROGRESS(3) });
    renderWithApp(<LearnScreen {...PROPS} />);
    expect(await screen.findByText("3 de 3")).toBeInTheDocument();
    expect(screen.getAllByText("hecho")).toHaveLength(3);
  });
});

describe("Markdown", () => {
  it("renders text, drops raw HTML and images, and keeps only safe links", () => {
    renderWithApp(<Markdown source={'Hi <b onclick="x()">bold</b> ![pic](https://example.com/a.png) [ok](https://example.com) [bad](javascript:alert(1))'} />);
    expect(document.querySelector("b")).toBeNull();
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByRole("link", { name: "ok" })).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.queryByRole("link", { name: "bad" })).not.toBeInTheDocument();
  });
});

describe.each([
  ["es", homeWords("es")],
  ["en", homeWords("en")],
] as const)("Home (%s)", (locale, words) => {
  const HOME = {
    ready: true,
    owner_name: "Ana Perez",
    fund_name: "Fondo Uno",
    engine: "claude_code",
    claude_status: "connected",
    agents: { active: 5, total: 10 },
    projects: 3,
    usage: { used: 250, cap: 1000, pct: 25 },
  };

  it("greets with the first name and shows the real numbers and the three Learn cards", async () => {
    setLocale(locale);
    mockApi({ "GET /api/home": HOME, "GET /api/learn/progress": PROGRESS(2) });
    renderWithApp(<HomeScreen {...PROPS} />);
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(words.greeting);
    expect(screen.getByText(words.fund)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: words.agents })).toHaveTextContent("5 / 10");
    expect(screen.getByRole("region", { name: words.cap })).toHaveTextContent("25%");
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "25");
    expect(await screen.findByText(t("learn.firstDay.progress", { done: 2, total: 3 }, locale))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: words.talk })).toBeInTheDocument();
    expect(screen.queryByText(/pr.xima clase|next class|cohorte|cohort/i)).not.toBeInTheDocument();
  });
});

describe("Home states", () => {
  it("asks to define the weekly allowance when there is none", async () => {
    mockApi({ "GET /api/home": { ready: true, owner_name: "", fund_name: "", engine: "claude_code", claude_status: "no_session", agents: { active: 0, total: 0 }, projects: 0, usage: { used: 0, cap: null, pct: null } }, "GET /api/learn/progress": PROGRESS(0) });
    renderWithApp(<HomeScreen {...PROPS} />);
    expect(await screen.findByText(t("home.cap.unset", undefined, "es"))).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/^Buen/);
    expect(screen.getByText(t("home.claude.no_session", undefined, "es"))).toBeInTheDocument();
    expect(screen.queryByText(/^Tu fondo:/)).not.toBeInTheDocument();
  });

  it("explains that the database is not ready yet", async () => {
    mockApi({ "GET /api/home": { ready: false } });
    renderWithApp(<HomeScreen {...PROPS} />);
    expect(await screen.findByText(t("home.notReady.title", undefined, "es"))).toBeInTheDocument();
  });

  it("shows the error with retry", async () => {
    mockApi({ "GET /api/home": failure(500, "Home failed") });
    renderWithApp(<HomeScreen {...PROPS} />);
    await waitFor(() => expect(screen.getByText("Home failed")).toBeInTheDocument());
  });
});
