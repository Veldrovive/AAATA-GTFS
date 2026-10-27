"""
Fits the bus position model to the clean trajectories of one shape (by MLE,
full-batch L-BFGS, no covariates) and prints a summary of the fitted
parameters. Needs the `model` extra (`uv sync --extra model`) and an OSM
signals blob (`scripts/fetch_osm_signals.py`).
"""
import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import torch

from aaata_gtfs import db
from aaata_gtfs.config import DATA_PATH, LOCAL_TZ, db_path
from aaata_gtfs.model.chain import default_params
from aaata_gtfs.model.data import by_layout
from aaata_gtfs.model.fit import fit_lbfgs, mean_log_likelihood
from aaata_gtfs.model.forward import ForwardModel, Parameters
from aaata_gtfs.osm import latest_signals_blob, load_signals
from aaata_gtfs.queries import data_time_range
from aaata_gtfs.shapes import GeometryResolver
from aaata_gtfs.static_gtfs import StaticGtfs
from aaata_gtfs.trips import reconstruct_from_db


def parse_time(value: str) -> float:
    t = datetime.fromisoformat(value)
    return (t if t.tzinfo else t.replace(tzinfo=LOCAL_TZ)).timestamp()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fit the bus position model to one shape's trajectories.")
    parser.add_argument("--data-path", type=Path, default=DATA_PATH, help=f"Data directory (default: {DATA_PATH})")
    parser.add_argument("--start", type=parse_time, help="ISO-8601 (local if naive; default: the earliest data)")
    parser.add_argument("--end", type=parse_time, help="ISO-8601 (local if naive; default: the latest data)")
    parser.add_argument("--shape-id", help="GTFS shape id (default: the one with the most trajectories)")
    parser.add_argument("--max-trips", type=int, default=20, help="Use at most this many trips (default: 20)")
    parser.add_argument("--max-iter", type=int, default=50, help="L-BFGS evaluations (default: 50)")
    parser.add_argument("--threads", type=int, help="torch CPU threads")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.threads:
        torch.set_num_threads(args.threads)

    path = db_path(args.data_path)
    if not path.exists():
        sys.exit(f"no database at {path}")
    blob = latest_signals_blob(args.data_path)
    if blob is None:
        sys.exit("no OSM signals; run scripts/fetch_osm_signals.py first")
    session = db.connect(path, read_only=True)
    data_range = data_time_range(session)
    if data_range is None:
        sys.exit("no positions in the database")
    start = args.start if args.start is not None else data_range[0]
    end = args.end if args.end is not None else data_range[1]
    static = StaticGtfs(session)
    resolver = GeometryResolver(session, static, args.data_path)
    trajectories = reconstruct_from_db(session, resolver, start, end).trajectories
    groups = by_layout(trajectories, load_signals(blob), static)
    if args.shape_id is not None:
        groups = [g for g in groups if g[0].key.endswith(f":{args.shape_id}")]
    if not groups:
        sys.exit("no trajectories for that shape")
    layout, trips = max(groups, key=lambda g: len(g[1]))
    trips = trips[: args.max_trips]
    print(f"{layout.key}: {layout.n_cells} cells of {layout.cell_ft:g} ft, {len(layout.stops)} stops, "
          f"{len(layout.lights)} lights; {len(trips)} trips, {sum(len(t.steps) for t in trips)} observations")

    model = ForwardModel(layout)
    params = Parameters(layout).set_from(default_params(layout))
    before = mean_log_likelihood(model, params, trips)
    t0 = time.time()
    history = fit_lbfgs(model, params, trips, max_iter=args.max_iter)
    after = mean_log_likelihood(model, params, trips)
    print(f"mean log-lik per observation: {before:.3f} -> {after:.3f} "
          f"({len(history)} evaluations, {time.time() - t0:.0f} s)")

    fitted = params.to_params()
    advance = np.arange(layout.max_advance + 1) * layout.cell_ft
    speeds = fitted.move_probs @ advance / 1.4667  # ft/s -> mph
    print(f"observation noise {fitted.sigma_ft:.1f} ft, outliers {fitted.outlier:.2%}")
    print(f"mean moving speed by segment (mph): median {np.median(speeds):.1f}, "
          f"range {speeds.min():.1f}–{speeds.max():.1f}")
    phases = np.arange(fitted.n_phases, 0, -1)
    red_wait = (fitted.light_pi @ phases) / fitted.light_q if len(layout.lights) else np.array([])
    dwell = (fitted.dwell_pi @ phases) / fitted.dwell_q if len(layout.stops) else np.array([])
    if len(red_wait):
        print(f"lights: P(red) median {np.median(fitted.p_red):.2f}, mean wait | red median {np.median(red_wait):.0f} s")
    if len(dwell):
        print(f"stops: P(stop) median {np.median(fitted.p_stop):.2f}, mean dwell | stop median {np.median(dwell):.0f} s")


if __name__ == "__main__":
    main()
