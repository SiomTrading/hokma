# Event research desk

A desk for dated events: results releases, regulatory filings and moves of large holders. It keeps a calendar of what is
coming, reads each event against what was expected and reviews afterwards what the market did with it.

## What this folder holds

- `events/calendar.md`: the events the desk follows, with date, symbol and what to read.
- `events/review.md`: the review of each event once it has passed, kept as written at the time.
- `reports/`, `journal/`, `data/`, `notes/`, `tasks/`: the common structure of every desk.

## Who owns what

| Role | Owns |
|---|---|
| Desk chief | The calendar and the order of work around each event. |
| Data agent | Filings, calendars and prices, always with the cut-off time of the view. |
| Research agent | The reading of each event: what was reported, what was expected, what changed. |
| Quant modules | The numbers: surprise, drift and the other criteria of the line. |
| Judge | The verdict on whether the reading holds. |
| Reviewer | Checks that a review uses only what was known before the event when it claims to predict it. |
| Owner | Rules, limits, mandate, capital and budget, in the app. |

## Approved versions

This template lists its approved versions in `template.json`, each with the digest of its exact content. A version that
does not match its digest is not applied.

## Rules of the desk

1. A review is written from the data of its cut-off time. Later data goes in a separate note.
2. The fiscal quarter comes from the name of the report, never from the calendar date.
3. A rumor can be listed as context. It is never the basis of a view.
