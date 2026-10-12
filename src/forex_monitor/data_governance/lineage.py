"""Acyclic dataset lineage with upstream and impact traversal."""

from __future__ import annotations

from collections import deque
from threading import RLock
from typing import Dict, Iterable, Optional, Tuple

from forex_monitor.data_governance.models import LineageEdge
from forex_monitor.errors import StorageConflictError, ValidationError


class LineageGraph:
    def __init__(self) -> None:
        self._edges: Dict[str, LineageEdge] = {}
        self._outgoing: Dict[str, list[str]] = {}
        self._incoming: Dict[str, list[str]] = {}
        self._lock = RLock()

    def add(self, edge: LineageEdge) -> LineageEdge:
        if not isinstance(edge, LineageEdge):
            raise ValidationError("edge must be LineageEdge")
        with self._lock:
            existing = self._edges.get(edge.edge_id)
            if existing is not None:
                if existing == edge:
                    return existing
                raise StorageConflictError("lineage edge id already exists")
            if self._reachable(edge.target_dataset_id, edge.source_dataset_id):
                raise ValidationError("lineage edge would create a cycle")
            self._edges[edge.edge_id] = edge
            self._outgoing.setdefault(edge.source_dataset_id, []).append(edge.edge_id)
            self._incoming.setdefault(edge.target_dataset_id, []).append(edge.edge_id)
            return edge

    def remove(self, edge_id: str) -> bool:
        with self._lock:
            edge = self._edges.pop(edge_id, None)
            if edge is None:
                return False
            self._outgoing[edge.source_dataset_id].remove(edge.edge_id)
            self._incoming[edge.target_dataset_id].remove(edge.edge_id)
            return True

    def upstream(self, dataset_id: str, *, max_depth: Optional[int] = None) -> Tuple[str, ...]:
        return self._traverse(dataset_id, self._incoming, "source_dataset_id", max_depth)

    def downstream(self, dataset_id: str, *, max_depth: Optional[int] = None) -> Tuple[str, ...]:
        return self._traverse(dataset_id, self._outgoing, "target_dataset_id", max_depth)

    def direct_inputs(self, dataset_id: str) -> Tuple[LineageEdge, ...]:
        with self._lock:
            return tuple(self._edges[item] for item in self._incoming.get(dataset_id, ()))

    def direct_outputs(self, dataset_id: str) -> Tuple[LineageEdge, ...]:
        with self._lock:
            return tuple(self._edges[item] for item in self._outgoing.get(dataset_id, ()))

    def field_sources(self, dataset_id: str, field: str) -> Tuple[Tuple[str, str], ...]:
        result = []
        with self._lock:
            for edge_id in self._incoming.get(dataset_id, ()):
                edge = self._edges[edge_id]
                for source, target in edge.field_mappings:
                    if target == field:
                        result.append((edge.source_dataset_id, source))
        return tuple(sorted(set(result)))

    def _traverse(self, start: str, adjacency: Dict[str, list[str]], attribute: str, max_depth: Optional[int]) -> Tuple[str, ...]:
        if max_depth is not None and max_depth < 1:
            raise ValidationError("lineage depth must be positive")
        visited = set()
        queue = deque([(start, 0)])
        with self._lock:
            while queue:
                current, depth = queue.popleft()
                if max_depth is not None and depth >= max_depth:
                    continue
                for edge_id in adjacency.get(current, ()):
                    target = getattr(self._edges[edge_id], attribute)
                    if target not in visited:
                        visited.add(target)
                        queue.append((target, depth + 1))
        return tuple(sorted(visited))

    def _reachable(self, start: str, target: str) -> bool:
        return target in self._traverse(start, self._outgoing, "target_dataset_id", None)
