"""Progress of "Your first day with Hokma", read from `onboarding_progress` plus what the database already shows.

Step names are the contract with the services that complete them (chat, projects):
    FIRST_CONVERSATION  a conversation with at least one message
    FIRST_PROJECT       the owner created a project
    FIRST_IMAGE         an image was attached
`unit.onboarding.mark(STEP)` is idempotent. When a step is not marked yet, the data decides: a conversation with
messages, an image attachment, or a second project (the first one is made by Hokma) mark it here."""
from hokma.db import current_version, get_database

FIRST_CONVERSATION = "first_conversation"
FIRST_PROJECT = "first_project"
FIRST_IMAGE = "first_image"
STEPS = (FIRST_CONVERSATION, FIRST_PROJECT, FIRST_IMAGE)
SCAN_LIMIT = 500


def _derive(unit) -> set[str]:
    found: set[str] = set()
    if any(c.message_count > 0 for c in unit.conversations.list(limit=SCAN_LIMIT)):
        found.add(FIRST_CONVERSATION)
    if len(unit.projects.list()) >= 2:
        found.add(FIRST_PROJECT)
    if unit.attachments.list(kind="image", limit=1):
        found.add(FIRST_IMAGE)
    return found


def collect() -> dict:
    if current_version() == 0:
        return {"ready": False, "done": 0, "total": len(STEPS), "steps": {s: False for s in STEPS}}
    with get_database().write() as unit:
        marked = unit.onboarding.progress(STEPS).steps
        for step in _derive(unit):
            if not marked[step]:
                unit.onboarding.mark(step)
                marked[step] = True
    return {"ready": True, "done": sum(marked.values()), "total": len(STEPS), "steps": marked}
