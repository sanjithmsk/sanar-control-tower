"""What a domain pack provides.

The engine is industry-neutral: sites, items, lots, lanes, suppliers and countries. A pack adds the
vocabulary, the handling requirements, who may approve what, policy defaults and a sample network
for one industry. Adding an industry means adding a pack, not changing the engine.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Requirement:
    """A handling rule attached to items by tag.

    Sites, lanes and suppliers must carry the capability `cap` (default: the tag itself), so a new
    rule (frozen, hazmat, halal, high value...) is a line of data rather than new code.
    """
    label: str
    badge: str = ""              # short pill text in the console; empty hides the tag
    cap: str = ""                # capability required; defaults to the tag
    site: bool = False           # storage at both ends must carry the capability
    lane: bool = False           # the lane / vehicle must carry it
    supplier: bool = False       # the supplier must be certified for it
    temp: tuple | None = None    # (min °C, max °C) checked on receipt; also caps transit time
    tolerance_min: int = 0       # minutes outside the range tolerated on receipt
    freeze_below: float | None = None  # any reading below this damages the goods
    permit: bool = False         # imports need a valid permit for the item category
    signoff: tuple = ()          # roles that must approve any action on the item
    ref: str = ""                # governing document


@dataclass(frozen=True)
class Domain:
    name: str
    title: str
    tagline: str
    labels: dict                 # item, items, site, sites, lot, demand
    roles: dict                  # role id -> label; the first one is the console default
    approvals: dict              # sign-off reason -> roles allowed to approve it
    requirements: dict           # tag -> Requirement
    sample: dict                 # sample network loaded by db.reset
    policy: dict = field(default_factory=dict)  # overrides of guardrails.POLICY defaults
    refs: dict = field(default_factory=dict)    # document ids quoted in guardrail messages
    currency: str = "SAR"

    @property
    def default_role(self):
        return next(iter(self.roles))
