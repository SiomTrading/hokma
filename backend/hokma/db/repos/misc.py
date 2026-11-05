"""onboarding_progress and consumption (usage)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from hokma.db.errors import ValidationError
from hokma.db.models import Progress, UsageRow, UsageTotal, WeekUsage
from hokma.db.repos.base import Repo
from hokma.db.timeutil import FORMAT, from_db_time, to_db_time, utcnow


class OnboardingRepo(Repo):
    def mark(self, step: str) -> None:
        if not 1 <= len(step or "") <= 80:
            raise ValidationError("a step name has 1 to 80 characters")
        self.s.execute("INSERT INTO onboarding_progress (step, done_at) VALUES (:s, :t) ON CONFLICT (step) DO NOTHING",
                       {"s": step, "t": utcnow()})

    def unmark(self, step: str) -> None:
        self.s.execute("DELETE FROM onboarding_progress WHERE step = :s", {"s": step})

    def done(self) -> dict[str, str]:
        return {r["step"]: r["done_at"] for r in self.s.query("SELECT * FROM onboarding_progress ORDER BY done_at, step")}

    def progress(self, steps: Sequence[str]) -> Progress:
        done = self.done()
        flags = {step: step in done for step in steps}
        return Progress(done=sum(flags.values()), total=len(flags), steps=flags)


class UsageRepo(Repo):
    """Tokens from messages (chat) and task_runs (agent work). Read-only."""

    @staticmethod
    def _range(column: str, since: Any, until: Any) -> tuple[str, dict[str, Any]]:
        sql, params = "", {}
        if since:
            sql += f" AND {column} >= :since"
            params["since"] = to_db_time(since)
        if until:
            sql += f" AND {column} < :until"
            params["until"] = to_db_time(until)
        return sql, params

    def total(self, *, since: Any = None, until: Any = None) -> UsageTotal:
        mw, mp = self._range("created_at", since, until)
        rw, rp = self._range("started_at", since, until)
        m = self.s.one(f"SELECT COALESCE(SUM(tokens_in), 0) AS i, COALESCE(SUM(tokens_out), 0) AS o, count(*) AS n "
                       f"FROM messages WHERE 1 = 1{mw}", mp) or {}
        r = self.s.one(f"SELECT COALESCE(SUM(tokens_in), 0) AS i, COALESCE(SUM(tokens_out), 0) AS o, count(*) AS n "
                       f"FROM task_runs WHERE 1 = 1{rw}", rp) or {}
        tin, tout = int(m["i"]) + int(r["i"]), int(m["o"]) + int(r["o"])
        return UsageTotal(tokens_in=tin, tokens_out=tout, tokens=tin + tout, messages=int(m["n"]), runs=int(r["n"]))

    def _grouped(self, msg_key: str, run_key: str, since: Any, until: Any, order: str = "tokens DESC, k") -> list[UsageRow]:
        mw, mp = self._range("created_at", since, until)
        rw, rp = self._range("started_at", since, until)
        sql = (f"SELECT k, SUM(i) AS tokens_in, SUM(o) AS tokens_out FROM ("
               f"SELECT {msg_key} AS k, tokens_in AS i, tokens_out AS o FROM messages WHERE 1 = 1{mw} "
               f"UNION ALL SELECT {run_key} AS k, tokens_in AS i, tokens_out AS o FROM task_runs WHERE 1 = 1{rw}) u "
               f"WHERE k IS NOT NULL GROUP BY k ORDER BY {order.replace('tokens', 'SUM(i) + SUM(o)')}")
        return [UsageRow(key=r["k"], tokens_in=int(r["tokens_in"]), tokens_out=int(r["tokens_out"]))
                for r in self.s.query(sql, {**mp, **rp})]

    def by_agent(self, *, since: Any = None, until: Any = None) -> list[UsageRow]:
        return self._grouped("agent_slug", "agent_slug", since, until)

    def by_model(self, *, since: Any = None, until: Any = None) -> list[UsageRow]:
        return self._grouped("model", "model", since, until)

    def by_day(self, *, since: Any, until: Any = None) -> list[UsageRow]:
        if self.s.engine == "postgres":                           # TIMESTAMPTZ; the session runs in UTC
            msg_day, run_day = "to_char(created_at, 'YYYY-MM-DD')", "to_char(started_at, 'YYYY-MM-DD')"
        else:
            msg_day, run_day = "substr(created_at, 1, 10)", "substr(started_at, 1, 10)"
        return self._grouped(msg_day, run_day, since, until, order="k")

    def week(self, *, now: Any = None) -> WeekUsage:
        row = self.s.one("SELECT timezone, weekly_token_cap FROM profile WHERE id = 1") or {}
        zone: Any = timezone.utc
        try:
            from zoneinfo import ZoneInfo

            if row.get("timezone"):
                zone = ZoneInfo(row["timezone"])
        except Exception:                                         # unknown zone or no tz database: UTC
            zone = timezone.utc
        moment = from_db_time(to_db_time(now) or utcnow()).astimezone(zone)
        start_local = (moment - timedelta(days=moment.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=7)
        start = start_local.astimezone(timezone.utc).strftime(FORMAT)
        end = end_local.astimezone(timezone.utc).strftime(FORMAT)
        used = self.total(since=start, until=end).tokens
        cap = row.get("weekly_token_cap")
        return WeekUsage(used=used, cap=cap, pct=round(used * 100.0 / cap, 1) if cap else None,
                         starts_at=start, ends_at=end)
