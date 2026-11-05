import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import {
  Bubble,
  Button,
  EmptyState,
  ErrorState,
  Field,
  Glyph,
  IconButton,
  Input,
  Loading,
  Menu,
  MenuItem,
  Meter,
  Modal,
  NavItem,
  Option,
  Pill,
  Segmented,
  Select,
  Steps,
  Tab,
  Toaster,
  Toggle,
} from "@/design";

function Root({ children }: { children: React.ReactNode }) {
  return <div className="hk">{children}</div>;
}

describe("buttons and indicators", () => {
  it("maps variants and sizes to the design classes", () => {
    render(
      <Root>
        <Button>a</Button>
        <Button variant="primary" size="sm">b</Button>
        <Button variant="ghost">c</Button>
        <Button variant="danger">d</Button>
      </Root>,
    );
    expect(screen.getByText("a")).toHaveClass("btn");
    expect(screen.getByText("b")).toHaveClass("btn", "pri", "sm");
    expect(screen.getByText("c")).toHaveClass("ghost");
    expect(screen.getByText("d")).toHaveClass("bad");
  });

  it("IconButton has an accessible name and a pressed state", () => {
    render(<Root><IconButton icon="terminal" label="Terminal" on /></Root>);
    const btn = screen.getByRole("button", { name: "Terminal" });
    expect(btn).toHaveClass("ib", "on");
    expect(btn).toHaveAttribute("aria-pressed", "true");
  });

  it("Toggle flips through onChange and exposes switch semantics", async () => {
    const onChange = vi.fn();
    render(<Root><Toggle checked={false} onChange={onChange} label="Light mode" /></Root>);
    const sw = screen.getByRole("switch", { name: "Light mode" });
    expect(sw).toHaveAttribute("aria-checked", "false");
    await userEvent.click(sw);
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("Pill tones, Meter clamping and Glyph", () => {
    render(
      <Root>
        <Pill tone="ok">ok</Pill>
        <Meter value={140} label="usage" />
        <Glyph initial="X" />
      </Root>,
    );
    expect(screen.getByText("ok")).toHaveClass("pill", "ok");
    expect(screen.getByRole("progressbar", { name: "usage" })).toHaveAttribute("aria-valuenow", "100");
    expect(screen.getByText("X")).toHaveClass("glyph");
  });

  it("Bubble, NavItem and Tab render their roles", async () => {
    const onClose = vi.fn();
    render(
      <Root>
        <Bubble from="user">hi</Bubble>
        <NavItem icon="chat" label="Chat" active kbd="Ctrl N" />
        <Tab label="One" active onSelect={() => {}} onClose={onClose} closeLabel="Close One" />
      </Root>,
    );
    expect(screen.getByText("hi")).toHaveClass("bub", "u");
    expect(screen.getByRole("button", { name: "Chat" })).toHaveAttribute("aria-current", "page");
    await userEvent.click(screen.getByRole("button", { name: "Close One" }));
    expect(onClose).toHaveBeenCalled();
  });
});

describe("selection controls", () => {
  it("Segmented marks the selected value and reports changes", async () => {
    const onChange = vi.fn();
    render(
      <Root>
        <Segmented label="Range" value="a" onChange={onChange} options={[{ value: "a", label: "A" }, { value: "b", label: "B" }]} />
      </Root>,
    );
    expect(screen.getByRole("button", { name: "A" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: "B" }));
    expect(onChange).toHaveBeenCalledWith("b");
  });

  it("Option behaves as a radio card", async () => {
    const onSelect = vi.fn();
    render(<Root><div role="radiogroup"><Option selected onSelect={onSelect}>Card</Option></div></Root>);
    const radio = screen.getByRole("radio", { name: "Card" });
    expect(radio).toBeChecked();
    await userEvent.click(radio);
    expect(onSelect).toHaveBeenCalled();
  });

  it("Steps mark done, current and pending", () => {
    render(<Root><Steps labels={["One", "Two", "Three"]} current={1} /></Root>);
    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveClass("done");
    expect(items[1]).toHaveClass("on");
    expect(items[1]).toHaveAttribute("aria-current", "step");
    expect(items[2]).not.toHaveClass("on", "done");
  });
});

describe("forms", () => {
  it("Field links label, control and error message", () => {
    render(
      <Root>
        <Field label="Name" error="Required">
          {(p) => <Input {...p} />}
        </Field>
      </Root>,
    );
    const input = screen.getByLabelText("Name");
    expect(input).toHaveClass("in");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription("Required");
  });

  it("Select keeps native options", async () => {
    render(
      <Root>
        <Select aria-label="Pick" defaultValue="b">
          <option value="a">A</option>
          <option value="b">B</option>
        </Select>
      </Root>,
    );
    const select = screen.getByLabelText("Pick") as HTMLSelectElement;
    expect(select.value).toBe("b");
    await userEvent.selectOptions(select, "a");
    expect(select.value).toBe("a");
  });
});

describe("overlays", () => {
  function MenuHarness() {
    const [open, setOpen] = useState(true);
    return (
      <Root>
        <button type="button">outside</button>
        <Menu open={open} onClose={() => setOpen(false)} label="Options">
          <MenuItem selected>First</MenuItem>
          <MenuItem hint="h">Second</MenuItem>
        </Menu>
      </Root>
    );
  }

  it("Menu closes on Escape and on outside click", async () => {
    const { unmount } = render(<MenuHarness />);
    expect(screen.getByRole("menu", { name: "Options" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    unmount();

    render(<MenuHarness />);
    fireEvent.pointerDown(screen.getByText("outside"));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("Menu moves focus with the arrow keys", async () => {
    render(<MenuHarness />);
    const [first, second] = screen.getAllByRole("menuitem");
    first!.focus();
    fireEvent.keyDown(first!, { key: "ArrowDown" });
    expect(second).toHaveFocus();
  });

  it("Modal is a labelled dialog that closes with Escape and the backdrop, and restores focus", async () => {
    const onClose = vi.fn();
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    const { unmount } = render(
      <Root>
        <Modal label="Settings" onClose={onClose}>
          <button type="button">inside</button>
        </Modal>
      </Root>,
    );
    const dialog = screen.getByRole("dialog", { name: "Settings" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.mouseDown(dialog.parentElement!);
    expect(onClose).toHaveBeenCalledTimes(2);
    unmount();
    expect(opener).toHaveFocus();
    opener.remove();
  });

  it("only the topmost layer reacts to Escape", async () => {
    const closeModal = vi.fn();
    const closeMenu = vi.fn();
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <Root>
          <Modal label="Dialog" onClose={closeModal}>
            <button type="button" onClick={() => setOpen(true)}>open-inner</button>
            <Menu open={open} onClose={() => { closeMenu(); setOpen(false); }} label="Inner"><MenuItem>x</MenuItem></Menu>
          </Modal>
        </Root>
      );
    }
    render(<Harness />);
    await userEvent.click(screen.getByText("open-inner"));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(closeMenu).toHaveBeenCalledTimes(1);
    expect(closeModal).not.toHaveBeenCalled();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(closeModal).toHaveBeenCalledTimes(1);
  });
});

describe("states", () => {
  it("EmptyState renders title, description and action", () => {
    render(<Root><EmptyState title="Nothing" description="Add one" action={<Button>Add</Button>} /></Root>);
    expect(screen.getByRole("heading", { name: "Nothing" })).toBeInTheDocument();
    expect(screen.getByText("Add one")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add" })).toBeInTheDocument();
  });

  it("Loading announces itself with the translated label", () => {
    render(<Root><Loading /></Root>);
    expect(screen.getByRole("status")).toHaveTextContent("Cargando");
  });

  it("ErrorState shows the message and retries", async () => {
    const retry = vi.fn();
    render(<Root><ErrorState message="Boom" onRetry={retry} /></Root>);
    expect(screen.getByRole("alert")).toHaveTextContent("Boom");
    await userEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(retry).toHaveBeenCalled();
  });

  it("Toaster lists notices and dismisses them", async () => {
    const dismiss = vi.fn();
    render(<Root><Toaster dismissLabel="Dismiss" onDismiss={dismiss} items={[{ id: 7, tone: "bad", text: "Failed" }]} /></Root>);
    expect(screen.getByRole("alert")).toHaveTextContent("Failed");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(dismiss).toHaveBeenCalledWith(7);
  });
});
