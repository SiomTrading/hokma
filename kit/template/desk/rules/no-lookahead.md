# No lookahead

A view may use only what was known at its cut-off time. The clause has two forms. The desk picks the form by its mode
and the prompt builder inserts the cut-off time where the text says [AS_OF].

## Backtest

Treat [AS_OF] as the present moment. Use only data stamped at or before it. Do not use any knowledge of anything that
happened after it, even if you remember it. If a statement in the data refers to a later date, ignore it and say so.

## Live

The present moment is [AS_OF]. Use only data received at or before it. Anything dated later than [AS_OF] is not part of
this view. Say what you do not have yet instead of guessing it.
