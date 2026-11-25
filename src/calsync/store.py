"""Provider-neutral calendar store interface used by the sync engine.

One ``CalendarStore`` is one CalDAV collection (an iCloud calendar or a Google calendar).
The engine only ever talks to this interface, so it can be tested against in-memory fakes,
a local Radicale and (read-only first) the real providers.

Write primitives are *conditional by construction*:
  create  -> If-None-Match: *   (can never overwrite an existing resource)
  update  -> If-Match: <etag>   (can never overwrite a concurrent edit)
  delete  -> If-Match: <etag>   (can never delete a concurrent edit)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class StoreError(Exception):
    """Provider/transport failure. ``transient`` errors are retried by the scheduler."""

    def __init__(self, msg: str, *, transient: bool = True, status: int | None = None):
        super().__init__(msg)
        self.transient = transient
        self.status = status


class AuthError(StoreError):
    """Credentials rejected / OAuth refresh failed (e.g. invalid_grant). Never transient."""

    def __init__(self, msg: str, *, status: int | None = None):
        super().__init__(msg, transient=False, status=status)


class RateLimited(StoreError):
    def __init__(self, msg: str, *, retry_after: float | None = None, status: int | None = 429):
        super().__init__(msg, transient=True, status=status)
        self.retry_after = retry_after


class PreconditionFailed(StoreError):
    """If-Match / If-None-Match precondition failed: the remote changed underneath us."""

    def __init__(self, msg: str = "precondition failed"):
        super().__init__(msg, transient=False, status=412)


@dataclass(frozen=True)
class Item:
    href: str          # stable server-side identifier of the resource (path or opaque id)
    etag: str          # opaque version token; "" is invalid
    data: bytes        # complete raw iCalendar resource exactly as the server stores it


class CalendarStore(Protocol):
    name: str

    def list_items(self) -> list[Item]:
        """Return EVERY resource of the collection with its data.

        Must be all-or-nothing: if any listed resource cannot be fetched, or the response
        is truncated/inconsistent, raise StoreError. A silently partial listing would look
        like mass deletion to the engine."""

    def create(self, data: bytes) -> Item:
        """Create a new resource (If-None-Match: *). Returns the item as stored (re-read)."""

    def update(self, href: str, etag: str, data: bytes) -> Item:
        """Replace resource ``href`` iff its etag still equals ``etag``. Returns re-read item."""

    def delete(self, href: str, etag: str) -> None:
        """Delete resource iff its etag still equals ``etag``."""
