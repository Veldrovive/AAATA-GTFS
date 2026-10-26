"""Observations for the model: trajectories as (step, distance) series on a layout."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from aaata_gtfs.model.chain import NO_HOLD
from aaata_gtfs.model.layout import CELL_FT, MAX_ADVANCE, Layout, build_layout
from aaata_gtfs.osm import Signal, lights_on_shape
from aaata_gtfs.service_dates import schedule_ts
from aaata_gtfs.static_gtfs import StaticGtfs
from aaata_gtfs.trips import Trajectory


@dataclass(frozen=True)
class TripObservations:
    """One trip's observations: distances at steps (seconds) after its first fix."""

    steps: np.ndarray  # int, increasing, starting at 0
    dist_ft: np.ndarray
    #: For each of the layout's stops, the step of its scheduled departure if
    #: it's a timepoint (the bus holds until then), else `NO_HOLD`.
    sched_steps: np.ndarray
    start_ts: int = 0
    trip_id: str | None = None
    service_date: str | None = None
    vehicle_id: str | None = None
    #: Simulated trips only: the noise-free distance at every step.
    true_dist_ft: np.ndarray | None = None


def trip_observations(trajectory: Trajectory, layout: Layout, static: StaticGtfs) -> TripObservations:
    """A trajectory's raw projected distances (the model has its own noise
    model, so not the isotonic fit), with its timepoints' scheduled departures."""
    start = trajectory.start_ts
    trip = static.trip(trajectory.trip_id, trajectory.service_date)
    stop_times = {} if trip is None else {
        st.stop_sequence: st for st in static.stop_times(trip.version, trajectory.trip_id)
    }
    sched = []
    for stop in layout.stops:
        st = stop_times.get(stop.stop_sequence) if stop.stop_sequence is not None else None
        if st is None or st.timepoint != 1 or st.departure_s is None:
            sched.append(NO_HOLD)
        else:
            sched.append(math.ceil(schedule_ts(trajectory.service_date, st.departure_s) - start))
    return TripObservations(
        np.array([p.fix_ts - start for p in trajectory.pings], dtype=int),
        np.array([p.raw_dist_ft for p in trajectory.pings], dtype=float),
        np.array(sched, dtype=int),
        start, trajectory.trip_id, trajectory.service_date, trajectory.vehicle_id,
    )


@dataclass(frozen=True)
class ObservationBatch:
    """Trips on one layout, padded to a common number of steps."""

    y: np.ndarray  # [B, n_steps + 1], distance where observed (0 elsewhere)
    mask: np.ndarray  # [B, n_steps + 1] bool
    sched_steps: np.ndarray  # [B, n_stops]

    @property
    def size(self) -> int:
        return self.y.shape[0]


def make_batch(trips: Sequence[TripObservations]) -> ObservationBatch:
    n_steps = max(int(t.steps[-1]) for t in trips)
    y = np.zeros((len(trips), n_steps + 1))
    mask = np.zeros((len(trips), n_steps + 1), dtype=bool)
    for b, t in enumerate(trips):
        y[b, t.steps] = t.dist_ft
        mask[b, t.steps] = True
    return ObservationBatch(y, mask, np.stack([t.sched_steps for t in trips]))


def by_layout(
    trajectories: Sequence[Trajectory],
    signals: Sequence[Signal],
    static: StaticGtfs,
    cell_ft: float = CELL_FT,
    max_advance: int = MAX_ADVANCE,
) -> list[tuple[Layout, list[TripObservations]]]:
    """Trajectories grouped by layout (shape and stop list), each with its
    observations; layouts in order of first appearance."""
    groups: dict[tuple[str, tuple[str, ...]], tuple[Layout, list[TripObservations]]] = {}
    for trajectory in trajectories:
        geometry = trajectory.geometry
        key = (geometry.key, tuple(s.stop_id for s in geometry.stops))
        if key not in groups:
            groups[key] = (build_layout(geometry, lights_on_shape(geometry, signals), cell_ft, max_advance), [])
        layout, trips = groups[key]
        trips.append(trip_observations(trajectory, layout, static))
    return list(groups.values())
