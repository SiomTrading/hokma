"""The anonymous sample of the account (SPEC-026 v2, piece 3390e1): every id is XXXX, the amounts are intact, nothing personal is written."""
from __future__ import annotations

import json

import pytest
from schwab_support import FAKE_ACCOUNT_NUMBER, FAKE_ACCOUNT_NUMBER_2, FAKE_HASH, FAKE_HASH_2, load
from test_broker_schwab_accounts import TR, connections, fake, keys, server  # noqa: F401  (fixtures)

from hokma.schwab import sample

SECRETS = [FAKE_ACCOUNT_NUMBER, FAKE_ACCOUNT_NUMBER_2, FAKE_HASH, FAKE_HASH_2]


def test_ids_become_xxxx_and_amounts_are_not_touched():
    raw = [{"securitiesAccount": {"type": "MARGIN", "accountNumber": FAKE_ACCOUNT_NUMBER, "hashValue": FAKE_HASH, "displayAcctId": "9988",
                                   "initialBalances": {"equity": 25000.5, "accountValue": 25000.5},
                                   "positions": [{"instrument": {"symbol": "SPY", "cusip": "037833100", "instrumentId": 761}, "longQuantity": 10,
                                                  "currentDayProfitLoss": -34.0}],
                                   "note": f"opened with {FAKE_HASH} today"}}]
    out = sample.anonymize(raw, SECRETS)
    inner = out[0]["securitiesAccount"]
    assert inner["accountNumber"] == inner["hashValue"] == inner["displayAcctId"] == "XXXX"
    assert inner["initialBalances"] == {"equity": 25000.5, "accountValue": 25000.5}              # accountValue is an amount, not an id
    assert inner["positions"][0]["currentDayProfitLoss"] == -34.0 and inner["positions"][0]["instrument"]["cusip"] == "037833100"
    assert inner["note"] == "opened with XXXX today"                                                # a secret inside a text goes too
    assert FAKE_HASH not in json.dumps(out)


def test_the_activity_message_loses_the_account_and_the_long_numbers_but_not_the_amounts():
    activity = {"received_at": "2026-11-25T15:00:00Z", "entry": {"seq": 7, "key": "Account Activity", "1": FAKE_ACCOUNT_NUMBER, "2": "OrderFill",
                                                                 "3": "order 98765432101 account 12345678 filled 10 at 512.30 total 5123.00"}}
    text = sample.build([], activity, SECRETS)
    entry = json.loads(text)["account_activity_last"]["entry"]
    assert entry["1"] == "XXXX" and entry["2"] == "OrderFill" and entry["seq"] == 7
    assert entry["3"] == "order XXXX account XXXX filled 10 at 512.30 total 5123.00"


def test_a_file_that_would_keep_a_personal_value_is_not_written():
    with pytest.raises(RuntimeError):
        sample.build([{FAKE_ACCOUNT_NUMBER: 1}], None, SECRETS)                       # a number as the NAME of a field is not replaced: refused


def test_capture_writes_one_anonymous_file_with_the_answer_of_accounts(fake, tmp_path):
    activity = {"received_at": "2026-11-25T15:00:00Z", "entry": {"seq": 1, "key": "Account Activity", "1": FAKE_ACCOUNT_NUMBER, "2": "SUBSCRIBED", "3": ""}}
    result = sample.capture(activity, folder=tmp_path)
    assert result["ok"] and result["accounts"] == 2 and result["activity"] is True and result["file"].startswith(str(tmp_path))
    text = (tmp_path / result["file"].split("/")[-1]).read_text(encoding="utf-8")
    assert not any(secret in text for secret in SECRETS)
    document = json.loads(text)
    assert document["provisional"] is False and document["account_activity_last"]["entry"]["1"] == "XXXX"
    securities = document["accounts_fields_positions"][0]["securitiesAccount"]
    assert securities["accountNumber"] == "XXXX" and securities["currentBalances"]["equity"] == load("accounts.json")[0]["securitiesAccount"]["currentBalances"]["equity"]
    asked = [c for c in fake.calls if c.path == f"{TR}/accounts"]
    assert asked and asked[-1].query == {"fields": "positions"}
    assert all(c.method == "GET" for c in fake.calls)                                          # read only: no order, no write


def test_the_file_goes_to_the_downloads_folder_of_the_owner(monkeypatch, tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    monkeypatch.setenv("HOKMA_DOWNLOADS_DIR", str(folder))
    assert sample.downloads_dir() == (folder, True)
    monkeypatch.setenv("HOKMA_DOWNLOADS_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    found, is_downloads = sample.downloads_dir()
    assert is_downloads is False and found.name == "diagnostics"
