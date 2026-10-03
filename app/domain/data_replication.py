"""
Data-replication safety rules for failover / failback (V2.x) — pure domain.

Context: a replicated database (the banking ledger) streams from the primary
cluster to a hot standby on the secondary cluster. On failover the standby is
PROMOTED: it switches to a new timeline and accepts writes. The two dangers are:

1. SPLIT BRAIN — the old primary comes back (it was frozen, not destroyed) and
   its local writers resume writing to it. Two writable copies then diverge.
   Rule: when BOTH copies are writable, the one on the OLDER timeline is stale
   and must be FENCED (its application role can no longer log in).

2. PREMATURE FAILBACK — the workloads return to the primary cluster while its
   database is still the stale copy (or is being rebuilt). Rule: the primary
   cluster only counts as "recovered" for the failover controller when its
   database is writable, not fenced, and is the newest copy (the secondary is a
   standby again, or on an older timeline).

The timeline is PostgreSQL's own lineage counter: it increases at each promotion,
so it orders copies without comparing clocks.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DatabaseStatus:
    reachable: bool
    in_recovery: bool | None = None       # True = standby (read-only)
    timeline: int | None = None
    app_login: bool | None = None         # False = fenced
    replay_lag_seconds: float | None = None  # standby only: now() - last replayed commit
    lsn: str | None = None

    @property
    def writable(self) -> bool:
        return self.reachable and self.in_recovery is False


def stale_writable(a: DatabaseStatus, b: DatabaseStatus) -> bool:
    """True if `a` must be fenced: both copies writable and `a` is on an older timeline."""
    return (a.writable and b.writable and a.app_login is not False
            and a.timeline is not None and b.timeline is not None and a.timeline < b.timeline)


def failback_ready(primary: DatabaseStatus, secondary: DatabaseStatus) -> tuple[bool, str]:
    """Is the primary cluster's copy the authoritative, writable one again?"""
    if not primary.reachable:
        return False, "primary database unreachable"
    if primary.in_recovery:
        return False, "primary database is still a standby (resync in progress)"
    if primary.app_login is False:
        return False, "primary database is fenced (stale copy, resync required)"
    if secondary.writable and (primary.timeline or 0) <= (secondary.timeline or 0):
        return False, (f"secondary database is writable on timeline {secondary.timeline} ≥ "
                       f"primary's {primary.timeline}: data has not been failed back")
    return True, "primary database is the authoritative writable copy"
