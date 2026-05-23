from __future__ import annotations
from dataclasses import dataclass, field
from typing import Set, Iterable, Optional


@dataclass
class SeenSet:
    """
    Tracks which node ids have been assigned to some zone.

    Now optionally tracks the zone_id for each node
    (not required by the placement engine, but helpful for debugging).
    """
    node_ids: Set[str] = field(default_factory=set)
    node_to_zone: dict[str, str] = field(default_factory=dict)

    def add(self, nid: str, zone_id: Optional[str] = None) -> None:
        if not nid:
            return
        self.node_ids.add(nid)
        if zone_id is not None:
            self.node_to_zone[nid] = zone_id

    def add_many(self, nids: Iterable[str], zone_id: Optional[str] = None) -> None:
        for n in (nids or []):
            self.add(n, zone_id)

    def zone_of(self, nid: str) -> Optional[str]:
        return self.node_to_zone.get(nid)

    def __contains__(self, nid: str) -> bool:
        return nid in self.node_ids
