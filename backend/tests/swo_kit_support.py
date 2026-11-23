"""A small fake kit for the SWO desk tests: one agent file per seat of the plantel, and the skills some of them declare."""
from pathlib import Path

from hokma.desks.plantel import SEATS

SKILL = """---
name: {name}
version: 1.0.0
description: >
  Use when the desk reads {name}. Trigger phrases: "read {name}".
identity: An analyst who reads {name} and says what it means.
patterns:
  - Read the figure before the opinion.
avoid:
  - Using any figure published after the cut-off.
---

1. Call the tools of the seat.
"""

AGENT = """---
name: {slug}
display_name: {name}
role: specialist
slug: {slug}
description: >
  Seat {code} of a desk.
model: sonnet
hokma-tier: balanced
lines: [{lines}]
skills: [{skills}]
---

You are {name}, seat {code}.
"""


def build_kit(root: Path, *, skills: dict[str, list[str]] | None = None, lines: dict[str, str] | None = None) -> Path:
    """Write the kit under `root`: agents/, skills/ and mandates/ (copied from the real kit). `skills` maps a seat code to the
    skills its file declares; `lines` overrides the lines a seat file declares."""
    skills = skills or {}
    lines = lines or {}
    (root / "agents").mkdir(parents=True, exist_ok=True)
    declared = sorted({s for names in skills.values() for s in names})
    for name in declared:
        (root / "skills" / name).mkdir(parents=True, exist_ok=True)
        (root / "skills" / name / "SKILL.md").write_text(SKILL.format(name=name), encoding="utf-8")
    for seat in SEATS.values():
        text = AGENT.format(slug=seat.slug, name=seat.name, code=seat.code, lines=lines.get(seat.code, "SWO, SWC, LP"),
                            skills=", ".join(skills.get(seat.code, [])))
        (root / "agents" / f"{seat.slug}.md").write_text(text, encoding="utf-8")
    real = Path(__file__).resolve().parents[2] / "kit" / "template" / "desk" / "mandates"
    (root / "mandates").mkdir(exist_ok=True)
    for file in real.glob("*.yaml"):
        (root / "mandates" / file.name).write_text(file.read_text(encoding="utf-8"), encoding="utf-8")
    return root
