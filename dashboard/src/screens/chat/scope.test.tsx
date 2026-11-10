import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { t } from "@/i18n";
import { ChatHub } from "./hub";
import type { HubClient } from "./hub";
import { ProjectScope } from "./ProjectScope";
import type { ProjectOption, ProjectRef } from "./types";

const many = (n: number): ProjectOption[] => Array.from({ length: n }, (_, i) => ({ id: String(i + 1), name: `Project ${i + 1}`, kind: "strategy" }));

function mount(projects: ProjectOption[], value: ProjectRef | null = null, onCreate = vi.fn()) {
  const onChange = vi.fn();
  render(<ProjectScope projects={projects} value={value} onChange={onChange} onCreate={onCreate} />);
  return { onChange, onCreate };
}

describe("U-12: the picker with no, some and many projects", () => {
  it("without projects: only General and a link to create one", async () => {
    const { onCreate } = mount([]);
    expect(screen.getAllByRole("radio")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: t("chat.scope.create") }));
    expect(onCreate).toHaveBeenCalled();
  });

  it("with 12 projects all show; with 15: 11 icons and 'See all' with the 15", async () => {
    mount(many(12));
    expect(screen.getAllByRole("radio")).toHaveLength(13); // General + 12
    expect(screen.queryByRole("button", { name: t("chat.scope.more") })).toBeNull();
  });

  it("15 projects", async () => {
    const { onChange } = mount(many(15));
    expect(screen.getAllByRole("radio")).toHaveLength(12); // General + 11
    await userEvent.click(screen.getByRole("button", { name: t("chat.scope.more") }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getAllByRole("button", { name: /^(Elegir|Pick) Project/ })).toHaveLength(15);
    await userEvent.type(within(dialog).getByRole("searchbox"), "Project 15");
    expect(within(dialog).getAllByRole("button", { name: /^(Elegir|Pick) Project/ })).toHaveLength(1);
    await userEvent.click(within(dialog).getByRole("button", { name: /Project 15/ }));
    expect(onChange).toHaveBeenCalledWith({ id: "15", name: "Project 15" });
  });

  it("General is marked when nothing was chosen, and the chosen project stays visible even if it was behind 'See all'", () => {
    mount(many(15), { id: "14", name: "Project 14" });
    expect(screen.getByRole("radio", { name: "Project 14" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: t("chat.scope.general") })).toHaveAttribute("aria-checked", "false");
  });
});

describe("U-14: three drafts waiting at once", () => {
  it("each `conversation` event reaches the draft whose client_ref it carries, whatever the order", () => {
    let push: (data: unknown) => void = () => undefined;
    const hub = new ChatHub((_path, handlers = {}) => {
      push = (data) => handlers.onMessage?.(data);
      return { send: () => true, close: () => undefined };
    });
    const seen: Record<string, string[]> = { a: [], b: [], c: [] };
    const clients: HubClient[] = ["a", "b", "c"].map((ref) => ({
      id: null as string | null,
      ref,
      awaiting: true as boolean,
      onEvent: (event) => seen[ref]!.push(event.type + ":" + ("conversation_id" in event ? event.conversation_id : "")),
      onOpen: () => undefined,
      claim(id) {
        this.id = id;
        this.awaiting = false;
      },
    }));
    clients.forEach((c) => hub.register(c));
    const created = (id: string, ref: string) => ({ type: "conversation", conversation_id: id, client_ref: ref, title: "", model: "sonnet", agent: null, project: null, created: true });
    act(() => {
      push(created("30", "c"));
      push(created("10", "a"));
      push(created("20", "b"));
    });
    expect(clients.map((c) => c.id)).toEqual(["10", "20", "30"]);
    expect(seen).toEqual({ a: ["conversation:10"], b: ["conversation:20"], c: ["conversation:30"] });
    // an error of one creation goes to its draft only
    act(() => push({ type: "error", code: "unknown_model", message: "x", client_ref: "zzz" }));
    expect(seen.a).toHaveLength(1);
  });
});
