# Adding a broker connector

A connector lets a desk trade through one outside broker. This page is the recipe, and the conformance suite is the judge: a connector is done
when the suite is green for it, and not before.

## What a connector is

- A class in `backend/hokma/broker/` that obeys the `Broker` protocol of `contract.py` and declares, in `capabilities`, what it can do.
- One `brokers.kind` word. The service opens the class by that word (`external.CONNECTORS`, used by `service._open`).
- The only module of Hokma that imports the library of that broker. A test scans the backend and fails when another module does.
- It reads and translates. It never decides whether an order may go out: the switch, the desk state, the limits and the owner levels are in
  `hokma.broker.rules` and `hokma.exec`, and the connector never turns the orders switch of its broker on.

## Steps

1. **Pick the kind and the key entries.** One lower case word (`kind`). One credential store entry for the keys, one for the fingerprint key.
   Keys live only in the system credential store, never in a file, the database, a log or an exception text.
2. **Subclass `external.ExternalBroker`.** Give it `capabilities`, `key_entry` and `live_identifiers()` (the identifiers the provider lists now,
   read from the provider). The base class gives you `account_ref`, `mask_of`, a refusal to reset or create accounts, and the matching of a local
   account with the provider's by fingerprint.
3. **Make the client injectable.** A module level `set_client_factory` (or `set_transport`) is the only door to the network, so a test can answer
   recorded replies. Build the client lazily, once per broker object, and never keep the broker object outside the database unit it was built in.
4. **Read the account.** `balance` returns `None` for cash and equity when the provider cannot be read, never a made up zero.
   `positions` returns signed quantities (short is negative) with the canonical `contract_id` when Hokma has one. `orders` and `get_order` return
   `Order` objects whose `data` went through `external.Scrubber.public`.
5. **Take the neutral order.** `hokma.exec.submit.to_native` gives every kind other than Schwab the same dict. Read it with
   `external.parse_order(order, provider)`: the provider symbol and the asset type come from the canonical contract id, never from what the caller wrote,
   and every amount is a `Decimal`. Translate it to the provider call. Convert to a float or an int only at the call of the library.
6. **Refuse, do not cut.** What the provider cannot do (a bracket, a multi-leg order, a time in force, a fractional contract) is answered with
   `external.refused("reason_word")` and nothing is sent. The reason is a stable word, never the text of the provider.
   A `reduce_only` order is cut to the position the provider reports at that moment (`external.reduce_legs`); the flag itself is never believed.
7. **Answer in the shared shape.** `place_order` and `replace_order` return `{"order_id", "status", "filled_qty"?, "avg_price"?, "reason"?}` with the
   status words `WORKING`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `EXPIRED`, `REJECTED`. `cancel_order` raises `NotSupported` when the order is not
   working. A provider that does not answer raises `ConnectorError("unavailable")` before anything is sent.
8. **Keep identifiers out.** The account identifier is used for the fingerprint (`external.sync_accounts`) and for the provider calls, nothing else.
   Everything that leaves the class (a model, an answer, an error, a log line, a row) carries `account_ref` and `mask` instead.
9. **Register it.** Add the kind to `external.CONNECTORS` and `external.BROKER_NAMES`. Add the file to `SEND_FILES` of
   `backend/tests/exec/test_send_scan.py` (and the library to `LIBRARY_FILES`): the order methods are called only by `hokma.exec.submit.dispatch`.
10. **Record answers.** Write made up JSON under `backend/tests/broker/fixtures/` with a sentinel string where the account identifier would be.
    No real data, no key. Add a harness in `backend/tests/broker/harnesses_connectors.py` through `register`.
11. **Run the suite.** `backend/tests/broker/test_contract_conformance.py` runs the same cases against every registered broker: accounts, balance,
    positions, orders, reading an order back, cancelling, and the sentinel check. A method the connector declares must work, a method it does not
    declare must raise `OrdersNotSupported`, and a connector that lies about either fails (`test_connector_capabilities.py` shows how).
12. **Run the rules.** `python3 scripts/check_repo_rules.py` must pass: English only, no em dash, no names of origin outside the notices file.

## Definition of done

- The conformance suite is green for the new kind, with recorded answers and no network.
- A test per refusal reason shows that nothing was sent.
- The unreachable provider case raises `unavailable` and sends nothing.
- No sentinel appears in the database, the answers or the logs.
- The scan of order calls and library imports is green.
- The credits of the library are in `.notices/THIRD_PARTY.txt`.
