"""A shape cut into cells, with the stops and lights a bus meets along it.

The model's state space is built on this (see `chain.py`). The shape is cut
into cells of `cell_ft`; cell `c` covers `[c * cell_ft, (c + 1) * cell_ft)`
and the last cell is the end of the trip (absorbing). Point elements (stops
and traffic lights) each sit in one cell, at most one per cell; the cells
between consecutive elements form a road segment with its own pace.

The trip's last stop isn't an element: arriving at the end cell ends the trip.
"""

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from aaata_gtfs.geometry import ShapeGeometry
from aaata_gtfs.osm import LightPosition

log = logging.getLogger(__name__)

#: Default cell length (ft). A bus at 30 mph covers ~1.8 cells a second.
CELL_FT = 25.0

#: Default most cells a bus can advance in one 1 s step (75 ft/s, ~51 mph).
MAX_ADVANCE = 3


@dataclass(frozen=True)
class Element:
    kind: Literal["stop", "light"]
    cell: int
    dist_ft: float
    #: The GTFS stop_id, or the light's OSM node ids joined by "+".
    ref: str
    stop_sequence: int | None = None


@dataclass(frozen=True)
class Layout:
    """Cells and elements along one shape (with one trip's stop list)."""

    key: str  # the geometry's key
    cell_ft: float
    n_cells: int
    max_advance: int
    elements: tuple[Element, ...]  # sorted by cell

    @property
    def stops(self) -> list[Element]:
        return [e for e in self.elements if e.kind == "stop"]

    @property
    def lights(self) -> list[Element]:
        return [e for e in self.elements if e.kind == "light"]

    @property
    def n_segments(self) -> int:
        return len(self.elements) + 1

    @property
    def end_cell(self) -> int:
        return self.n_cells - 1

    def cell_of(self, dist_ft: float) -> int:
        return min(max(int(dist_ft // self.cell_ft), 0), self.end_cell)

    def cell_centers_ft(self) -> np.ndarray:
        return (np.arange(self.n_cells) + 0.5) * self.cell_ft

    def segment_of_cell(self) -> np.ndarray:
        """Each cell's segment: the number of elements at or before it, so a
        bus leaving an element is on the segment after it."""
        cells = np.array([e.cell for e in self.elements], dtype=int)
        return np.searchsorted(np.sort(cells), np.arange(self.n_cells), side="right")

    def next_stop_cell(self) -> np.ndarray:
        """For each cell, the first element cell after it, else the end cell."""
        cells = np.array(sorted(e.cell for e in self.elements) + [self.end_cell], dtype=int)
        return cells[np.searchsorted(cells, np.arange(self.n_cells), side="right").clip(max=len(cells) - 1)]

    def moves(self) -> tuple[np.ndarray, np.ndarray]:
        """`(dest, arrival)`, each `[n_cells, max_advance + 1]`: where a bus at
        cell `c` advancing `j` cells ends up (stopped short at the next
        element), and the index in `elements` of the element it arrives at
        there (-1 if none). The end cell only moves to itself."""
        nxt = self.next_stop_cell()
        element_at = {e.cell: i for i, e in enumerate(self.elements)}
        dest = np.zeros((self.n_cells, self.max_advance + 1), dtype=int)
        arrival = np.full_like(dest, -1)
        for c in range(self.n_cells):
            for j in range(self.max_advance + 1):
                d = c if c == self.end_cell else min(c + j, int(nxt[c]))
                dest[c, j] = d
                if d > c and d in element_at:
                    arrival[c, j] = element_at[d]
        return dest, arrival


def build_layout(
    geometry: ShapeGeometry,
    lights: Sequence[LightPosition] = (),
    cell_ft: float = CELL_FT,
    max_advance: int = MAX_ADVANCE,
) -> Layout:
    """Cells and elements for a shape: its stops (except the last) and lights.

    Elements are placed in order of distance; one that lands in a cell
    already taken moves to the next cell if that's free (and not the end),
    else it's dropped (logged).
    """
    n_cells = max(int(math.ceil(geometry.length_ft / cell_ft)), 2)
    candidates: list[Element] = []
    for stop in geometry.stops[:-1]:
        candidates.append(Element("stop", 0, stop.dist_along_ft, stop.stop_id, stop.stop_sequence))
    for light in lights:
        candidates.append(Element("light", 0, light.dist_along_ft, "+".join(map(str, light.osm_ids))))
    candidates.sort(key=lambda e: e.dist_ft)

    taken: set[int] = set()
    placed: list[Element] = []
    for e in candidates:
        cell = min(max(int(e.dist_ft // cell_ft), 0), n_cells - 1)
        if cell in taken:
            cell += 1
        if cell in taken or cell >= n_cells - 1:
            log.debug("%s: dropped %s %s at %.0f ft (no free cell)", geometry.key, e.kind, e.ref, e.dist_ft)
            continue
        taken.add(cell)
        placed.append(Element(e.kind, cell, e.dist_ft, e.ref, e.stop_sequence))
    placed.sort(key=lambda e: e.cell)
    return Layout(geometry.key, cell_ft, n_cells, max_advance, tuple(placed))
