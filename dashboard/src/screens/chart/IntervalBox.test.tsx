import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { IntervalBox } from "./IntervalBox";
import type { IntervalBoxProps } from "./IntervalBox";
import { DEFAULT_FAVORITES, toggleFavorite } from "./favorites";

interface Seen {
  applied: string[];
  closed: number;
}

function Host(props: Partial<IntervalBoxProps> & { seen: Seen }) {
  const { seen, ...rest } = props;
  const [favorites, setFavorites] = useState<string[]>([...DEFAULT_FAVORITES]);
  const [open, setOpen] = useState(true);
  const body = useRef<HTMLDivElement>(null);
  return (
    <div>
      <div ref={body} tabIndex={0} data-testid="chart-body">
        chart
      </div>
      {open ? (
        <IntervalBox
          value="5m"
          favorites={favorites}
          onApply={(interval) => seen.applied.push(interval)}
          onToggleFavorite={(interval) => setFavorites((list) => toggleFavorite(list, interval))}
          onClose={() => {
            seen.closed += 1;
            setOpen(false);
          }}
          returnFocusRef={body}
          {...rest}
        />
      ) : null}
      <output aria-label="favorites">{favorites.join(" ")}</output>
    </div>
  );
}

function show(props: Partial<IntervalBoxProps> = {}) {
  const seen: Seen = { applied: [], closed: 0 };
  renderWithApp(<Host seen={seen} {...props} />);
  return seen;
}

const field = () => screen.getByRole("combobox", { name: t("chart.interval.label") });

beforeEach(() => setLocale("en"));

describe("interval box", () => {
  it("opens with the focus in the field, lists the favorites and applies one with the arrows and Enter", async () => {
    const seen = show();
    expect(screen.getByRole("dialog", { name: "Interval" })).toBeInTheDocument();
    expect(field()).toHaveFocus();
    const list = screen.getByRole("listbox");
    expect(within(list).getAllByRole("option").map((o) => o.textContent)).toEqual(["1m", "5m", "15m", "1h", "1D"]);
    await userEvent.keyboard("{ArrowDown}{ArrowDown}");
    expect(field()).toHaveAttribute("aria-activedescendant", within(list).getAllByRole("option")[1]!.id);
    await userEvent.keyboard("{Enter}");
    expect(seen.applied).toEqual(["5m"]);
    expect(seen.closed).toBe(1);
    expect(screen.getByTestId("chart-body")).toHaveFocus();
  });

  it("suggests one interval per unit for a number and applies a typed interval normalized", async () => {
    const seen = show({ initialText: "7" });
    const options = within(screen.getByRole("listbox")).getAllByRole("option");
    expect(options.map((o) => o.querySelector(".mono")?.textContent)).toEqual(["7s", "7m", "7h", "7D", "7W"]);
    expect(field()).toHaveValue("7");
    await userEvent.clear(field());
    await userEvent.type(field(), "120m{Enter}");
    expect(seen.applied).toEqual(["2h"]);
  });

  it("shows the grammar errors with their exact texts", async () => {
    const seen = show();
    await userEvent.type(field(), "3M");
    expect(screen.getByRole("alert")).toHaveTextContent("Months are not available yet");
    await userEvent.clear(field());
    await userEvent.type(field(), "7x{Enter}");
    expect(screen.getByRole("alert")).toHaveTextContent("Write a number and a unit: 30s, 7m, 3h, 2D, 1W");
    expect(field()).toHaveAttribute("aria-invalid", "true");
    expect(seen.applied).toEqual([]);
    await userEvent.type(field(), "{Backspace}m");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("accepts the range shortcuts in the field", async () => {
    const seen = show();
    await userEvent.type(field(), "10r{Enter}");
    expect(seen.applied).toEqual(["r:10t"]);
  });

  it("builds a range in ticks or in price in the Range section (T-10 UI)", async () => {
    const seen = show();
    const size = screen.getByRole("textbox", { name: "Range size" });
    await userEvent.type(size, "0{Enter}");
    expect(screen.getByRole("alert")).toHaveTextContent(t("chart.interval.rangeInvalid"));
    await userEvent.clear(size);
    await userEvent.type(size, "10{Enter}");
    expect(seen.applied).toEqual(["r:10t"]);
  });

  it("builds a price range", async () => {
    const seen = show();
    await userEvent.click(screen.getByRole("button", { name: "price" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Range size" }), "2.50");
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(seen.applied).toEqual(["r:2.5p"]);
  });

  it("offers price only when the tick size is unknown", async () => {
    const seen = show({ ticksDisabledReason: "No tick size" });
    expect(screen.queryByRole("button", { name: "ticks" })).toBeNull();
    await userEvent.type(screen.getByRole("textbox", { name: "Range size" }), "5{Enter}");
    expect(seen.applied).toEqual(["r:5p"]);
  });

  it("pins and unpins with the star", async () => {
    show();
    await userEvent.type(field(), "7m");
    const star = screen.getByRole("button", { name: "Add to favorites" });
    expect(star).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(star);
    expect(screen.getByRole("status", { name: "favorites" })).toHaveTextContent("1m 5m 7m 15m 1h 1D");
    const pinned = screen.getByRole("button", { name: "Remove from favorites" });
    expect(pinned).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(pinned);
    expect(screen.getByRole("status", { name: "favorites" })).toHaveTextContent("1m 5m 15m 1h 1D");
  });

  it("shows seconds disabled with the reason when the source has no trades (T-8 UI)", async () => {
    const reason = t("market.err.seconds_need_ticks");
    const seen = show({ initialText: "15", secondsDisabledReason: reason });
    const options = within(screen.getByRole("listbox")).getAllByRole("option");
    const seconds = options[0]!;
    expect(seconds).toHaveTextContent("15s");
    expect(seconds).toHaveAttribute("aria-disabled", "true");
    expect(seconds).toHaveAttribute("title", reason);
    await userEvent.click(seconds);
    expect(seen.applied).toEqual([]);
    await userEvent.keyboard("{ArrowDown}");
    expect(field()).toHaveAttribute("aria-activedescendant", options[1]!.id); // the arrows skip it
    await userEvent.clear(field());
    await userEvent.type(field(), "15s{Enter}");
    expect(screen.getByRole("alert")).toHaveTextContent(reason);
    expect(seen.applied).toEqual([]);
  });

  it("closes with Esc and gives the focus back to the chart", async () => {
    const seen = show();
    await userEvent.keyboard("{Escape}");
    expect(seen.closed).toBe(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("chart-body")).toHaveFocus();
  });
});
