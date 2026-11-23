"""Lessons of the desks (SPEC-017, SVC-memory): what a desk learned from decisions that really executed.

A lesson is born from an executed order (`propose`), is measured from the real entry fill once its window has traded
(`settle`), can be promoted by the Hokma manager or the owner to the general memory every desk reads (`promote`), and is read
back as of a date without seeing anything that was not yet known (`for_desk`). Lessons are never deleted.
"""
from hokma.core import processguard

processguard.require(__name__)          # runtime limit: it reads orders and the database, only the Hokma server loads it

from hokma.lessons.pit import for_desk  # noqa: E402
from hokma.lessons.settle import settle  # noqa: E402
from hokma.lessons.store import (  # noqa: E402
    Lesson, LessonError, NotExecuted, NotPromotable, NotReady, UnknownLesson, promote, propose, retire)

__all__ = ["Lesson", "LessonError", "NotExecuted", "NotPromotable", "NotReady", "UnknownLesson", "for_desk", "promote",
           "propose", "retire", "settle"]
