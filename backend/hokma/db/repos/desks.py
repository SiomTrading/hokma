"""desks, desk_agents, desk_assets, desk_connections (SPEC-014). Rows only: folders, templates and rules live in hokma.desks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hokma.db.errors import ValidationError
from hokma.db.models import Record, UsageRow, UsageTotal
from hokma.db.repos.base import UNSET, Repo, given, require_in
from hokma.db.repos.misc import UsageRepo
from hokma.db.timeutil import utcnow

TEMPLATES = ("minimal", "standard", "debate", "custom")
STATUSES = ("active", "paused", "archived")
ROLES = ("chief", "data", "risk", "research", "signals", "execution", "reviewer",
         "bull", "bear", "judge", "strategy_dev", "custom")
MODELS = ("haiku", "sonnet", "opus")
MAX_ASSETS = 20
MIN_CAPITAL, MAX_CAPITAL = 100, 1_000_000_000


@dataclass(frozen=True, slots=True)
class Desk(Record):
    id: int
    project_id: int
    name: str
    focus: str | None
    template: str
    mode: str
    capital: int
    currency: str
    objective_pct: float | None
    mandate: str | None
    token_budget: int | None
    budget_alert_week: str | None
    budget_alert_level: int | None
    status: str
    sort_order: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DeskAgent(Record):
    BOOLS = ("active",)
    desk_id: int
    agent_slug: str
    role: str
    model: str
    active: bool
    added_at: str


@dataclass(frozen=True, slots=True)
class DeskAsset(Record):
    desk_id: int
    symbol: str
    note: str | None
    sort_order: int


@dataclass(frozen=True, slots=True)
class DeskConnection(Record):
    BOOLS = ("enabled",)
    desk_id: int
    provider_id: str
    enabled: bool


class DesksRepo(Repo):
    # ---- desks -----------------------------------------------------------------------------

    def create(self, project_id: int, name: str, *, capital: int, focus: str | None = None,
               template: str = "standard", objective_pct: float | None = None, mandate: str | None = None,
               token_budget: int | None = None, sort_order: int | None = None) -> Desk:
        """The row only. `mode` is not a parameter: the table fixes it at `simulation`."""
        name = (name or "").strip()
        if not 1 <= len(name) <= 80:
            raise ValidationError("name must have 1 to 80 characters")
        if focus is not None and len(focus) > 120:
            raise ValidationError("focus must have at most 120 characters")
        if mandate is not None and len(mandate) > 4000:
            raise ValidationError("mandate must have at most 4000 characters")
        require_in("template", template, TEMPLATES)
        if not isinstance(capital, int) or isinstance(capital, bool) or not MIN_CAPITAL <= capital <= MAX_CAPITAL:
            raise ValidationError(f"capital must be a whole number from {MIN_CAPITAL} to {MAX_CAPITAL}")
        if objective_pct is not None and not 0 <= objective_pct <= 100:
            raise ValidationError("objective_pct must be between 0 and 100")
        if token_budget is not None and token_budget <= 0:
            raise ValidationError("token_budget must be positive")
        if sort_order is None:
            sort_order = int(self.s.scalar("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM desks"))
        new_id = self.s.insert("desks", {
            "project_id": project_id, "name": name, "focus": focus, "template": template, "capital": capital,
            "objective_pct": objective_pct, "mandate": mandate, "token_budget": token_budget,
            "sort_order": sort_order, "created_at": utcnow(), "updated_at": utcnow()})
        return self.get(new_id)

    def get(self, id: int) -> Desk:
        return self._get("desks", "id", id, Desk)

    def find(self, id: int) -> Desk | None:
        return self._find("desks", "id", id, Desk)

    def by_project(self, project_id: int) -> Desk | None:
        return self._find("desks", "project_id", project_id, Desk)

    def list(self, *, status: str | None = None, include_archived: bool = False) -> list[Desk]:
        sql, params = "SELECT * FROM desks WHERE 1 = 1", {}
        if status:
            require_in("status", status, STATUSES)
            sql += " AND status = :status"
            params["status"] = status
        elif not include_archived:
            sql += " AND status <> 'archived'"
        return [Desk.from_row(r) for r in self.s.query(sql + " ORDER BY sort_order, id", params)]

    def update(self, id: int, *, name: Any = UNSET, focus: Any = UNSET, capital: Any = UNSET,
               objective_pct: Any = UNSET, mandate: Any = UNSET, token_budget: Any = UNSET,
               status: Any = UNSET, sort_order: Any = UNSET) -> Desk:
        self.get(id)
        values = given(name=name, focus=focus, capital=capital, objective_pct=objective_pct, mandate=mandate,
                       token_budget=token_budget, status=status, sort_order=sort_order)
        if "name" in values:
            values["name"] = (values["name"] or "").strip()
            if not 1 <= len(values["name"]) <= 80:
                raise ValidationError("name must have 1 to 80 characters")
        if values.get("focus") is not None and "focus" in values and len(values["focus"]) > 120:
            raise ValidationError("focus must have at most 120 characters")
        if values.get("mandate") is not None and "mandate" in values and len(values["mandate"]) > 4000:
            raise ValidationError("mandate must have at most 4000 characters")
        if "capital" in values:
            cap = values["capital"]
            if not isinstance(cap, int) or isinstance(cap, bool) or not MIN_CAPITAL <= cap <= MAX_CAPITAL:
                raise ValidationError(f"capital must be a whole number from {MIN_CAPITAL} to {MAX_CAPITAL}")
        if values.get("objective_pct") is not None and not 0 <= values["objective_pct"] <= 100:
            raise ValidationError("objective_pct must be between 0 and 100")
        if values.get("token_budget") is not None and values["token_budget"] <= 0:
            raise ValidationError("token_budget must be positive")
        if "status" in values:
            require_in("status", values["status"], STATUSES)
        if values:
            self.s.update("desks", {"id": id}, {**values, "updated_at": utcnow()})
        return self.get(id)

    def set_alert(self, id: int, week: str | None, level: int | None) -> None:
        """Remember the last budget warning, so it is written once per week and level."""
        self.get(id)
        self.s.update("desks", {"id": id}, {"budget_alert_week": week, "budget_alert_level": level})

    # ---- agents ----------------------------------------------------------------------------

    def add_agent(self, desk_id: int, agent_slug: str, role: str, model: str, *, active: bool = True) -> DeskAgent:
        require_in("role", role, ROLES)
        require_in("model", model, MODELS)
        self.s.insert_row("desk_agents", {"desk_id": desk_id, "agent_slug": agent_slug, "role": role, "model": model,
                                          "active": 1 if active else 0, "added_at": utcnow()})
        return self.get_agent(desk_id, agent_slug)

    def get_agent(self, desk_id: int, agent_slug: str) -> DeskAgent:
        row = self.s.one("SELECT * FROM desk_agents WHERE desk_id = :d AND agent_slug = :a",
                         {"d": desk_id, "a": agent_slug})
        if row is None:
            from hokma.db.errors import NotFound

            raise NotFound(f"desk agent {agent_slug!r} not found in desk {desk_id}")
        return DeskAgent.from_row(row)

    def find_agent(self, desk_id: int, agent_slug: str) -> DeskAgent | None:
        row = self.s.one("SELECT * FROM desk_agents WHERE desk_id = :d AND agent_slug = :a",
                         {"d": desk_id, "a": agent_slug})
        return DeskAgent.from_row(row) if row else None

    def list_agents(self, desk_id: int) -> list[DeskAgent]:
        rows = self.s.query("SELECT * FROM desk_agents WHERE desk_id = :d ORDER BY added_at, agent_slug", {"d": desk_id})
        return [DeskAgent.from_row(r) for r in rows]

    def chief(self, desk_id: int) -> DeskAgent | None:
        row = self.s.one("SELECT * FROM desk_agents WHERE desk_id = :d AND role = 'chief'", {"d": desk_id})
        return DeskAgent.from_row(row) if row else None

    def set_agent(self, desk_id: int, agent_slug: str, *, model: Any = UNSET, active: Any = UNSET) -> DeskAgent:
        self.get_agent(desk_id, agent_slug)
        values = given(model=model, active=active)
        if "model" in values:
            require_in("model", values["model"], MODELS)
        if "active" in values:
            values["active"] = 1 if values["active"] else 0
        if values:
            self.s.update("desk_agents", {"desk_id": desk_id, "agent_slug": agent_slug}, values)
        return self.get_agent(desk_id, agent_slug)

    def delete(self, id: int) -> None:
        """The desk row; its team, assets and connections go with it (ON DELETE CASCADE). Agents are never touched."""
        desk = self.get(id)
        self.s.execute("UPDATE journal_entries SET project_id = NULL WHERE project_id = :p", {"p": desk.project_id})
        self.s.execute("DELETE FROM desks WHERE id = :id", {"id": id})

    def remove_agent(self, desk_id: int, agent_slug: str) -> None:
        self.get_agent(desk_id, agent_slug)
        self.s.execute("DELETE FROM desk_agents WHERE desk_id = :d AND agent_slug = :a",
                       {"d": desk_id, "a": agent_slug})

    # ---- assets ----------------------------------------------------------------------------

    def list_assets(self, desk_id: int) -> list[DeskAsset]:
        rows = self.s.query("SELECT * FROM desk_assets WHERE desk_id = :d ORDER BY sort_order, symbol", {"d": desk_id})
        return [DeskAsset.from_row(r) for r in rows]

    def replace_assets(self, desk_id: int, assets: list[tuple[str, str | None]]) -> list[DeskAsset]:
        """Swap the whole list. Each item is `(symbol, note)`; the order given is the order kept."""
        if len(assets) > MAX_ASSETS:
            raise ValidationError(f"a desk has at most {MAX_ASSETS} assets")
        seen: set[str] = set()
        for symbol, note in assets:
            if not 1 <= len(symbol or "") <= 40:
                raise ValidationError("a symbol has 1 to 40 characters")
            if note is not None and len(note) > 200:
                raise ValidationError("an asset note has at most 200 characters")
            if symbol in seen:
                raise ValidationError(f"duplicate symbol: {symbol}")
            seen.add(symbol)
        self.get(desk_id)
        self.s.execute("DELETE FROM desk_assets WHERE desk_id = :d", {"d": desk_id})
        for position, (symbol, note) in enumerate(assets):
            self.s.insert_row("desk_assets", {"desk_id": desk_id, "symbol": symbol, "note": note,
                                              "sort_order": position})
        return self.list_assets(desk_id)

    # ---- connections -----------------------------------------------------------------------

    def list_connections(self, desk_id: int) -> list[DeskConnection]:
        rows = self.s.query("SELECT * FROM desk_connections WHERE desk_id = :d ORDER BY provider_id", {"d": desk_id})
        return [DeskConnection.from_row(r) for r in rows]

    def set_connection(self, desk_id: int, provider_id: str, enabled: bool) -> DeskConnection:
        if not 1 <= len(provider_id or "") <= 80:
            raise ValidationError("provider_id has 1 to 80 characters")
        self.get(desk_id)
        flag = 1 if enabled else 0
        changed = self.s.execute("UPDATE desk_connections SET enabled = :e WHERE desk_id = :d AND provider_id = :p",
                                 {"e": flag, "d": desk_id, "p": provider_id})
        if not changed:
            self.s.insert_row("desk_connections", {"desk_id": desk_id, "provider_id": provider_id, "enabled": flag})
        return DeskConnection(desk_id=desk_id, provider_id=provider_id, enabled=bool(flag))

    # ---- usage -----------------------------------------------------------------------------

    def _usage_sources(self, project_id: int, since: Any, until: Any) -> tuple[str, str, dict[str, Any]]:
        """The two token sources of one desk: chat messages (through their conversation) and task runs (through their
        task), both filtered by the desk project. Returns (messages select, runs select, params); the selects take
        the key expression as `{k}`."""
        mw, mp = UsageRepo._range("m.created_at", since, until)
        rw, rp = UsageRepo._range("r.started_at", since, until)
        msg = ("SELECT {k} AS k, m.tokens_in AS i, m.tokens_out AS o FROM messages m "
               "JOIN conversations c ON c.id = m.conversation_id WHERE c.project_id = :pid" + mw)
        run = ("SELECT {k} AS k, r.tokens_in AS i, r.tokens_out AS o FROM task_runs r "
               "JOIN tasks t ON t.id = r.task_id WHERE t.project_id = :pid" + rw)
        return msg, run, {"pid": project_id, **mp, **rp}

    def usage_total(self, project_id: int, *, since: Any = None, until: Any = None) -> UsageTotal:
        msg, run, params = self._usage_sources(project_id, since, until)
        m = self.s.one(f"SELECT COALESCE(SUM(i), 0) AS i, COALESCE(SUM(o), 0) AS o, count(*) AS n FROM ({msg.format(k='1')}) x",
                       params) or {}
        r = self.s.one(f"SELECT COALESCE(SUM(i), 0) AS i, COALESCE(SUM(o), 0) AS o, count(*) AS n FROM ({run.format(k='1')}) x",
                       params) or {}
        tin, tout = int(m["i"]) + int(r["i"]), int(m["o"]) + int(r["o"])
        return UsageTotal(tokens_in=tin, tokens_out=tout, tokens=tin + tout, messages=int(m["n"]), runs=int(r["n"]))

    def _usage_grouped(self, project_id: int, msg_key: str, run_key: str, since: Any, until: Any,
                       order: str) -> list[UsageRow]:
        msg, run, params = self._usage_sources(project_id, since, until)
        sql = (f"SELECT k, SUM(i) AS tokens_in, SUM(o) AS tokens_out FROM ("
               f"{msg.format(k=msg_key)} UNION ALL {run.format(k=run_key)}) u WHERE k IS NOT NULL GROUP BY k "
               f"ORDER BY {order}")
        return [UsageRow(key=r["k"], tokens_in=int(r["tokens_in"]), tokens_out=int(r["tokens_out"]))
                for r in self.s.query(sql, params)]

    def usage_by_agent(self, project_id: int, *, since: Any = None, until: Any = None) -> list[UsageRow]:
        # the chat stores no agent on its messages: a message belongs to the agent its conversation runs with
        return self._usage_grouped(project_id, "COALESCE(m.agent_slug, c.agent_slug)", "r.agent_slug", since, until,
                                   "SUM(i) + SUM(o) DESC, k")

    def usage_by_day(self, project_id: int, *, since: Any = None, until: Any = None) -> list[UsageRow]:
        if self.s.engine == "postgres":
            msg_day, run_day = "to_char(m.created_at, 'YYYY-MM-DD')", "to_char(r.started_at, 'YYYY-MM-DD')"
        else:
            msg_day, run_day = "substr(m.created_at, 1, 10)", "substr(r.started_at, 1, 10)"
        return self._usage_grouped(project_id, msg_day, run_day, since, until, "k")

    # ---- activity of the team ----------------------------------------------------------------

    def working_agents(self, project_id: int) -> set[str]:
        """Slugs with a task run of this desk project still running."""
        rows = self.s.query("SELECT DISTINCT r.agent_slug AS a FROM task_runs r JOIN tasks t ON t.id = r.task_id "
                            "WHERE t.project_id = :pid AND r.outcome = 'running'", {"pid": project_id})
        return {r["a"] for r in rows}

    def recent_activity(self, project_id: int, agent_slug: str, limit: int = 3) -> list[dict[str, Any]]:
        """The latest journal entries of one agent in this desk project, newest first."""
        rows = self.s.query("SELECT id, kind, outcome, title, created_at FROM journal_entries "
                            "WHERE project_id = :pid AND agent_slug = :a ORDER BY id DESC LIMIT :n",
                            {"pid": project_id, "a": agent_slug, "n": limit})
        return [dict(r) for r in rows]

    def set_conversation_model(self, project_id: int, agent_slug: str, model: str) -> int:
        """Point the conversations of this agent in this desk project at the desk model. Returns how many changed."""
        return int(self.s.execute("UPDATE conversations SET model = :m WHERE project_id = :pid AND agent_slug = :a "
                                  "AND (model IS NULL OR model <> :m)", {"m": model, "pid": project_id, "a": agent_slug}))
