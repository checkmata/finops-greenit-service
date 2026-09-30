"""
Namespace selection for the scheduler (V1 fix: the MVP only processed "default").

Pure function: discovery happens in the adapter (PromQL), selection rules here.
"""

from __future__ import annotations

import re
from typing import Iterable

# Platform namespaces are excluded by default: their cost is shared
# infrastructure, not a team's workload. Overridable via NAMESPACE_EXCLUDE_REGEX.
DEFAULT_EXCLUDE_REGEX = r"^(kube-.*|openshift.*|kepler)$"


def select_namespaces(
    discovered: Iterable[str],
    *,
    include_regex: str = ".*",
    exclude_regex: str = DEFAULT_EXCLUDE_REGEX,
    explicit: Iterable[str] | None = None,
) -> list[str]:
    """
    Return the sorted namespaces to analyse.

    - `explicit` (non-empty) wins: exactly those namespaces, no discovery filter.
      WHY: an operator listing namespaces by hand means it.
    - otherwise: discovered ∩ include ∖ exclude (full-match regexes).
    """
    explicit_list = [n.strip() for n in (explicit or []) if n and n.strip()]
    if explicit_list:
        return sorted(set(explicit_list))

    inc = re.compile(include_regex)
    exc = re.compile(exclude_regex) if exclude_regex else None
    return sorted({
        ns for ns in discovered
        if ns and inc.fullmatch(ns) and not (exc and exc.fullmatch(ns))
    })
