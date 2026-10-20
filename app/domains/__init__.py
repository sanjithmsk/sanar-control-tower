"""Domain packs, one per industry. Register a new pack here to make it selectable.

TOWER_DOMAIN picks the pack loaded on first start; the console can switch packs at any time
(that reloads the sample network for the chosen pack).
"""
import os

from .base import Domain, Requirement
from .logistics import LOGISTICS
from .pharmacy import PHARMACY

__all__ = ["Domain", "Requirement", "PACKS", "DEFAULT", "get"]

PACKS = {p.name: p for p in (PHARMACY, LOGISTICS)}
DEFAULT = os.environ.get("TOWER_DOMAIN", "pharmacy")


def get(name=None):
    return PACKS.get(name or DEFAULT) or PACKS["pharmacy"]
