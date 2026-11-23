---
name: filing-events
version: 1.0.0
description: >
  Use when the owner or another agent asks what a current report (8-K) of a company says, which event is behind a headline,
  or whether a filing confirms a news story. Trigger phrases: "what did the 8-K say", "is this headline in the filing",
  "which items did they report", "did the company file anything today", "what is the event behind the move".
identity: A news analyst who treats the filing as the record and the headline as a pointer, and reads the item numbers before the story.
patterns:
  - Pull the 8-K filings visible at the cut-off with the filings tool and read the item numbers each one reports before reading any headline.
  - Translate every item number with the item table below and say in one line what kind of event it is, then read the text of that item only when the kind needs it.
  - When a headline and a filing disagree about the kind of event, report the filing as the fact and the headline as a hint that did not match.
  - For a stock with an event, read the 8-K that carries the event, with its filing date and acceptance time, and say whether it was filed before or after the cut-off.
  - For a swing on headlines, list the headlines as hints and mark each one as confirmed, not confirmed or not yet filed.
  - For a long term thesis, read the 8-K items that touch a pillar of the thesis (leadership, financing, auditor, impairments) and hand them to the thesis tracker.
  - Treat an amendment as a correction of the original filing and say which filing it replaces.
avoid:
  - Calling an event confirmed because a headline says so when no filing visible at the cut-off carries it.
  - Reading a filing that was filed after the cut-off, or quoting a figure from an exhibit that was published later.
  - Treating the Regulation FD item or the exhibits item as news by itself, they only carry material.
  - Inventing the content of an item whose text was not returned by the tool, report the item as unread.
  - Writing a number that the filings tool did not return, including amounts in press releases that were not fetched.
---

# Filing events

A headline is somebody's summary. The current report is the record the company signed. This skill reads the record first,
and uses the headline only to know where to look.

## 1. Item table

| Item | Kind | What it tells you |
|---|---|---|
| 1.01 | material agreement | A binding agreement outside the ordinary course was signed. |
| 1.02 | agreement ended | A material agreement was terminated. |
| 1.03 | bankruptcy | Bankruptcy or receivership. |
| 2.01 | acquisition or disposal | A deal for assets was completed. |
| 2.02 | results | Results of operations. Usually paired with the exhibits item and a press release. |
| 2.03 | new obligation | A direct financial obligation was created. |
| 2.04 | acceleration | A debt was accelerated or a trigger event happened. |
| 2.05 | exit costs | Restructuring or exit activity with costs. |
| 2.06 | impairment | A material impairment was recorded. |
| 3.01 | listing notice | A notice of failure to meet a listing rule, or a delisting. |
| 3.02 | unregistered sale | Equity was sold outside a registration. |
| 3.03 | rights change | The rights of security holders changed. |
| 4.01 | auditor change | The auditor was dismissed or resigned. |
| 4.02 | non reliance | Past financial statements should no longer be relied on. |
| 5.01 | control change | Control of the company changed. |
| 5.02 | officer or director change | A senior officer or director left, arrived or changed pay. |
| 5.03 | governing documents | Charter or bylaws were amended. |
| 5.07 | vote result | Results of a vote of holders. |
| 7.01 | disclosure | A Regulation FD disclosure, often a presentation. |
| 8.01 | other event | Anything the company chose to report, read the text. |
| 9.01 | exhibits | Financial statements and exhibits that belong to the other items. |

An item that is not in the table is reported as unknown, with its number, never guessed.

## 2. Read

1. Call `data_filings` with the symbol, the form `8-K` and the cut-off. A filing filed after the cut-off never appears. If the answer is empty, say that no 8-K was visible, which is not the same as no event.
2. For each filing write the filing date, the acceptance time when present, the report date and the items.
3. Classify the items with the table. The classification is the one the desk code returns for the filing; do not reclassify it from the headline.
4. Read the text of an item only when its kind is results, officer or director change, other event, impairment, auditor change or non reliance.

## 3. Compare with the headline

| Case | What you write |
|---|---|
| The filing carries the kind the headline describes | Confirmed by the filing, with the item number. |
| The filing carries a different kind | The filing says the kind it carries. The headline is a hint that did not match. |
| No filing visible carries the event | Not yet filed. The headline stays a rumor until a filing appears. |

## 4. Report

1. One line per filing: date, items, kind.
2. One line per headline: confirmed, not confirmed or not yet filed, with the item number when confirmed.
3. List what you could not read as data gaps.
4. Do not say what the event means for the price and do not give a trade window. Say what the filing states.
