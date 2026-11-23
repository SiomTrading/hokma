---
name: economic-releases
version: 1.0.0
description: >
  Use when the owner or another agent asks when a macro release comes out, what a report holds, how a figure compares with what
  was expected, or whether an event is close to an order. Trigger phrases: "when is CPI", "what time is the jobs report",
  "did payrolls beat consensus", "how big was the surprise", "is there an event today".
identity: A reader of the economic calendar who states times, figures and gaps as they are, and says plainly when a figure is missing.
patterns:
  - Read the time of a release from the calendar tool and show it with its time zone; the catalog gives the usual hour in New York time, the calendar gives the day.
  - Show the four figures of a release side by side (actual, consensus, previous, revised) and write "no data" for any that the provider did not give.
  - Compute the surprise only with an actual, a consensus and a historical deviation that all exist, and show the three numbers beside the result.
  - Name the sub-figures of a report (for example the revision of prior months) before calling the headline good or bad.
  - When the calendar tool says the event risk is unverified, say so and treat it as a reason for care, not as an empty day.
avoid:
  - Stating how far or in which direction a market moves after a release, unless a study run in this app gives the number and the skill cites it.
  - Writing a consensus, a deviation or an actual from memory when the provider did not return it.
  - Treating an empty answer from a failed calendar read as a day without events.
  - Using a figure that was published after the moment the question is about.
---

# Economic releases

Use the catalog of releases to answer when and what. Use the calendar tool for the real day, the figures and the risk of an
event near an order. Use the event studies of the desk for anything about how markets reacted.

## 1. When a release comes out

1. Ask the calendar tool for the events of the range and countries the question is about.
2. For the usual hour, use the catalog (time in New York, daylight saving included). The rule of the day is exact for the jobs
   report (first Friday of the month) and for weekly jobless claims (every Thursday). For the other releases the day is
   nominal (for example the consumer price index around the 13th): holidays and the publisher's own calendar move it, so the
   calendar tool has the last word.
3. Report the time in UTC and in New York time. Say which one the owner's own zone is when it differs.

## 2. What a report holds

The catalog lists, for each release, the parts of the report that should be read with the headline. Examples:

- Jobs report: the change in jobs, the revision of the two prior months, the unemployment rate (U-3), the underemployment rate (U-6),
  average hourly earnings, average weekly hours, labor force participation.
- Consumer prices: headline, core (without food and energy), core services without housing, shelter and owners' equivalent rent.
- Manufacturing purchasing managers index: the headline (50 separates growth from contraction), new orders, production, employment,
  prices paid, supplier deliveries, inventories, new export orders.

Each release also has a class: leading (jobless claims, purchasing managers indexes, building permits), coincident (jobs,
industrial production, retail sales, gross domestic product) or lagging (consumer and producer prices, the price index of
personal consumption). The class says where the figure sits in the cycle. It does not say what a market will do.

## 3. The table of a release

Each row has four figures: actual, consensus, previous and revised. The provider may leave any of them empty.

- A consensus that is empty is `NO_DATA`. Never replace it with the previous figure or with a guess.
- An actual that is empty means the release has not happened or the provider has not filled it yet.

## 4. The standardized surprise

    surprise = (actual - consensus) / historical standard deviation of that release's surprises

Worked example with made-up numbers: actual 175, consensus 150 and a deviation of 50 give 25 / 50 = 0.5.
The sign says above or below the consensus; the size says how many typical surprises it is.
If the deviation is not available, the answer is `NO_DATA`: ask for it, do not estimate it.

Monthly changes can be shown as annual rates with ((1 + p / 100) ^ 12 - 1) * 100; a monthly 0.2 percent is 2.43 percent a year.
This is arithmetic, not a forecast.

## 5. What this skill does not claim

It gives no size, direction or duration of a market reaction to any release. Those numbers are measured from price data by the
event studies of the desk, with their sample and their dates. If a study exists, quote its result with its source. If it does not,
say that nothing has been measured yet.
