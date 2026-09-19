"""Shared account and quota data, independent of the UI and providers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Profile:
    id: str
    name: str
    home: str
    managed: bool
    provider: str = "codex"



@dataclass
class Window:
    used: float
    minutes: int
    reset: int | None



@dataclass
class Quota:
    five: Window | None
    week: Window | None
    checked_at: float
    plan: str = ""
