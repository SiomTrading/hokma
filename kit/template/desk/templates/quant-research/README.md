# Quantitative research desk

A desk that answers one market question at a time with computed evidence. Every number comes from a Hokma module, every
claim carries a citation and every verdict says what would change it.

## What this folder holds

- `research/brief.md`: the question, the universe, the period and what counts as an answer.
- `research/evidence.md`: the runs that were made, with the registry function, the inputs and the result of each.
- `reports/`: the reports the desk delivers. `journal/`: facts, results, blocks and alerts. `tasks/`: one file per task.

## Who owns what

| Role | Owns |
|---|---|
| Desk chief | The brief, the split into tasks and the check of what comes back. |
| Data agent | Fetching bars, fundamentals and filings with the data tools, with the cut-off time on every call. |
| Research agent | Reading the data and writing the findings and their limits. |
| Quant modules | Every calculation. No agent computes a metric by hand: it calls the registry. |
| Judge | Weighing the evidence and writing the verdict. |
| Reviewer | Checking sources, traceability of numbers and declared gaps before the report goes out. |
| Owner | Rules, limits, mandate, capital and budget, in the app. |

Orders never come from this desk. Real money moves only through the order tools, within the rules of the desk.

## Starting work

1. Write the brief. If the question, the universe or the period is unclear, ask the owner before any run.
2. Create one task per responsibility and state the cut-off time in each.
3. Record every run in the evidence file as it happens.
4. Deliver the report with its source list, and put positive and negative results in the journal.

## Version

The template is applied only when its version and its digest match the approved ones. A desk keeps the version it started
with; moving to another version is an explicit upgrade.
