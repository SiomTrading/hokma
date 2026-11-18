import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { DEFAULT_FAVORITES } from "./favorites";
import { IntervalBar } from "./IntervalBar";
import { TIMEFRAMES } from "./types";

function Host({ seen }: { seen: string[] }) {
  const [value, setValue] = useState("5m");
  const [open, setOpen] = useState(false);
  return (
    <div style={{ position: "relative" }}>
      <IntervalBar
        value={value}
        onChange={(next) => {
          seen.push(next);
          setValue(next);
        }}
        open={open}
        onOpenChange={setOpen}
      />
      <output aria-label="value">{value}</output>
    </div>
  );
}

function show() {
  const seen: string[] = [];
  renderWithApp(<Host seen={seen} />);
  return seen;
}

const buttons = () => within(screen.getByRole("group", { name: t("chart.timeframe") })).getAllByRole("button").map((b) => b.textContent);
const field = () => screen.getByRole("combobox", { name: t("chart.interval.label") });
const openBox = () => userEvent.click(screen.getByRole("button", { name: t("chart.interval.more") }));

beforeEach(() => {
  localStorage.clear();
  setLocale("es");
});

describe("interval bar", () => {
  it("lists the same intervals the Chart screen starts with, weekly included", () => {
    show();
    expect(buttons()).toEqual(["1m", "5m", "15m", "1h", "1D", "1W"]);
    expect(buttons()).toEqual([...DEFAULT_FAVORITES]);
    expect(TIMEFRAMES).toEqual(DEFAULT_FAVORITES);
  });

  it("applies a favorite with one click", async () => {
    const seen = show();
    await userEvent.click(screen.getByRole("button", { name: "1W" }));
    expect(seen).toEqual(["1W"]);
    expect(screen.getByLabelText("value")).toHaveTextContent("1W");
  });

  it("lets the student write 3h and then 1W in the same control", async () => {
    const seen = show();
    await openBox();
    await userEvent.type(field(), "3h{Enter}");
    expect(seen).toEqual(["3h"]);
    expect(screen.queryByRole("dialog")).toBeNull();
    await openBox();
    await userEvent.type(field(), "1w{Enter}");
    expect(seen).toEqual(["3h", "1W"]);
  });

  it("normalizes what is written, and builds a range from the same box", async () => {
    const seen = show();
    await openBox();
    await userEvent.type(field(), "120m{Enter}");
    await openBox();
    await userEvent.type(field(), "10r{Enter}");
    expect(seen).toEqual(["2h", "r:10t"]);
  });

  it("refuses an invalid or monthly value with a clear message and applies nothing", async () => {
    const seen = show();
    await openBox();
    await userEvent.type(field(), "3M");
    expect(screen.getByRole("alert")).toHaveTextContent(t("chart.interval.months"));
    await userEvent.clear(field());
    await userEvent.type(field(), "abc{Enter}");
    expect(screen.getByRole("alert")).toHaveTextContent(t("chart.interval.invalid"));
    expect(seen).toEqual([]);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
