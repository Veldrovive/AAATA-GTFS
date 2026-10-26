"""
Forward prediction on simulated trips: how far off is the model's position
forecast h seconds ahead, given the pings so far, from the true (simulated,
noise-free) position? Rebuilds a `simulation_study.py` run's route and true
parameters from its seed, simulates fresh evaluation trips, and forecasts
from every ping with the true parameters, each given fit (`--out` .npz files
of runs with the same seed and route), the uninformed starting values, and a
constant-speed baseline. Positions are only scored while the bus is still on
its trip. Prints the scores per horizon and plots them. Needs the `model` extra
(torch and matplotlib).
"""
import argparse
import csv
import math
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import torch

from aaata_gtfs.model.chain import ModelParams, ReferenceChain
from aaata_gtfs.model.data import TripObservations, make_batch
from aaata_gtfs.model.forward import ForwardModel, Parameters
from aaata_gtfs.model.simulate import HorizonScore, forecast_targets, score_forecasts, simulate_trips, study_setup

#: The categorical palette (validated for colour-vision deficiency), in fixed order.
COLOURS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]


def load_fitted(path: Path) -> ModelParams:
    f = np.load(path)
    return ModelParams(f["fitted_move_probs"], f["fitted_p_red"], f["fitted_light_pi"], f["fitted_light_q"],
                       f["fitted_p_stop"], f["fitted_dwell_pi"], f["fitted_dwell_q"], float(f["fitted_sigma_ft"]),
                       float(f["fitted_outlier"]))


def combine(parts: Sequence[list[HorizonScore]]) -> list[HorizonScore]:
    """Scores over several chunks of trips (weighted by the number scored)."""
    out = []
    for scores in zip(*parts):
        n = sum(s.n for s in scores)

        def mean(field: str) -> float:
            return sum(getattr(s, field) * s.n for s in scores if s.n) / n if n else float("nan")

        out.append(HorizonScore(scores[0].horizon_s, n, mean("crps_ft"), mean("median_abs_ft"),
                                mean("median_bias_ft"), mean("coverage")))
    return out


def model_scores(model: ForwardModel, theta: Parameters, trips: list[TripObservations], horizons: np.ndarray,
                 chunk: int) -> list[HorizonScore]:
    centres, end_ft = model.layout.cell_centers_ft(), model.layout.cell_centers_ft()[-1]
    parts = []
    for i in range(0, len(trips), chunk):
        part = trips[i:i + chunk]
        fc = model.forecast(theta, make_batch(part), horizons.tolist())
        truth, scored = forecast_targets(part, fc.trip, fc.origin_step, fc.horizons, end_ft)
        parts.append(score_forecasts(fc.probs, centres, truth, scored, fc.horizons))
    return combine(parts)


def constant_speed_scores(trips: list[TripObservations], speed_ftps: float, horizons: np.ndarray,
                          end_ft: float) -> list[HorizonScore]:
    """Point forecasts: the last ping plus a constant speed, stopping at the end."""
    trip = np.concatenate([np.full(len(t.steps), b) for b, t in enumerate(trips)])
    origin = np.concatenate([t.steps for t in trips])
    y = np.concatenate([t.dist_ft for t in trips])
    truth, scored = forecast_targets(trips, trip, origin, horizons, end_ft)
    pred = np.minimum(y[:, None] + speed_ftps * horizons[None], end_ft)
    out = []
    for k, h in enumerate(horizons):
        m = scored[:, k]
        if not m.any():
            out.append(HorizonScore(int(h), 0, *[float("nan")] * 4))
            continue
        err = pred[m, k] - truth[m, k]
        # A point forecast's CRPS is its absolute error; it has no interval.
        out.append(HorizonScore(int(h), int(m.sum()), float(np.abs(err).mean()), float(np.abs(err).mean()),
                                float(err.mean()), float("nan")))
    return out


def plot(results: dict[str, list[HorizonScore]], path: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    panels = [("median_abs_ft", "Mean |forecast median − true position| (ft)"),
              ("crps_ft", "CRPS (ft; lower is better)"),
              ("coverage", "Share of true positions in the 80% interval")]
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8), layout="constrained")
    for ax, (field, label) in zip(axes, panels):
        ends: list[tuple[float, float, str]] = []
        for (name, scores), colour in zip(results.items(), COLOURS):
            x = np.array([s.horizon_s for s in scores])
            v = np.array([getattr(s, field) for s in scores])
            if not np.isfinite(v).any():
                continue
            dashed = name.startswith("constant speed")
            ax.plot(x, v, color=colour, lw=2, ls="--" if dashed else "-", label=name)
            last = np.flatnonzero(np.isfinite(v))[-1]
            ends.append((float(v[last]), float(x[last]), name))
        if field == "coverage":
            ax.axhline(0.8, color="#8a8980", lw=1, ls=":")
            ax.set_ylim(0, 1)
        # Direct labels at the line ends (up to four lines), pushed apart so they don't overlap.
        if len(ends) <= 4:
            (low, high), (left, right) = ax.get_ylim(), ax.get_xlim()
            gap, placed = 0.06 * (high - low), []
            ends.sort()
            for y, _, _ in ends:
                placed.append(max(y, placed[-1] + gap) if placed else y)
            shift = np.mean([y for y, _, _ in ends]) - np.mean(placed)  # keep the labels centred on the ends
            for (_, x_end, name), y_label in zip(ends, placed):
                ax.text(x_end + 0.01 * (right - left), y_label + shift, name, fontsize=8, color="#3d3d3a",
                        va="center")
        ax.set_xlabel("Forecast horizon (s ahead of the last ping)")
        ax.set_title(label, fontsize=10, loc="left")
        ax.grid(color="#e4e3dc", lw=0.6)
        ax.spines[["top", "right"]].set_visible(False)
        ax.margins(x=0.12)
    axes[0].legend(frameon=False, fontsize=8, loc="upper left")
    fig.suptitle(title, fontsize=11, x=0.01, ha="left")
    fig.savefig(path, dpi=150)
    print(f"saved the plot to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Position forecast error against the horizon, on simulated trips.")
    parser.add_argument("fits", type=Path, nargs="*", help="--out .npz files of runs with this seed and route")
    parser.add_argument("--length-mi", type=float, default=2.0, help="As in the runs (default: 2 mi)")
    parser.add_argument("--phases", type=int, default=8, help="As in the runs (default: 8)")
    parser.add_argument("--seed", type=int, default=0, help="As in the runs")
    parser.add_argument("--eval-trips", type=int, default=100, help="Fresh evaluation trips (default: 100)")
    parser.add_argument("--max-horizon", type=int, default=900, help="Longest horizon, s (default: 900)")
    parser.add_argument("--min-share", type=float, default=0.2,
                        help="Drop horizons where fewer than this share of forecasts are scored (default: 0.2): "
                             "only positions still on the trip are, so the longest horizons are slow trips only")
    parser.add_argument("--horizon-step", type=int, default=15, help="Horizon spacing, s (default: 15)")
    parser.add_argument("--chunk", type=int, default=20, help="Trips forecast at once (memory)")
    parser.add_argument("--threads", type=int, help="torch CPU threads")
    parser.add_argument("--plot", type=Path, help="Save the plot here (.png)")
    parser.add_argument("--csv", type=Path, help="Save the scores here")
    args = parser.parse_args()
    if args.threads:
        torch.set_num_threads(args.threads)

    layout, true, timepoints, _ = study_setup(args.seed, args.length_mi, args.phases)
    for path in args.fits:
        saved = np.load(path)
        if not np.allclose(saved["true_move_probs"], true.move_probs) or not np.allclose(saved["true_p_red"], true.p_red):
            raise SystemExit(f"{path}: its true parameters don't match: check --seed, --length-mi and --phases")
    chain = ReferenceChain(layout, true)
    # Fresh trips, from a generator independent of the run's training and held-out draws.
    rng = np.random.default_rng([args.seed, 1])
    trips = simulate_trips(chain, args.eval_trips, rng, timepoints=timepoints)
    end_ft = layout.cell_centers_ft()[-1]
    # The baseline's speed: the median over other simulated trips of route length / time to the end.
    pilots = simulate_trips(chain, 50, rng, timepoints=timepoints)
    speed = float(np.median([
        (end_ft - p.true_dist_ft[0]) / np.argmax(p.true_dist_ft >= end_ft)  # pyrefly: ignore[unsupported-operation]
        for p in pilots
    ]))
    horizons = np.arange(0, args.max_horizon + 1, args.horizon_step)
    print(f"route: {args.length_mi:g} mi, {layout.n_cells} cells; {len(trips)} evaluation trips, "
          f"{sum(len(t.steps) for t in trips)} forecast origins; constant-speed baseline {speed / 5280 * 3600:.1f} mph")

    model = ForwardModel(layout)
    sets: dict[str, Parameters] = {"truth": Parameters(layout, n_phases=args.phases).set_from(true)}
    for path in args.fits:
        sets[f"fit: {path.stem}"] = Parameters(layout, n_phases=args.phases).set_from(load_fitted(path))
    start = Parameters(layout, n_phases=args.phases)
    with torch.no_grad():
        start.log_sigma.fill_(math.log(48.0))  # as simulation_study.py starts its fits
    sets["start (uninformed)"] = start
    results = {name: model_scores(model, theta, trips, horizons, args.chunk) for name, theta in sets.items()}
    results["constant speed"] = constant_speed_scores(trips, speed, horizons, end_ft)
    n_scored = np.array([s.n for s in results["truth"]])
    cut = int(np.argmax(n_scored < args.min_share * n_scored[0])) or len(horizons)
    horizons = horizons[:cut]
    results = {name: scores[:cut] for name, scores in results.items()}
    print(f"scored horizons up to {horizons[-1]} s (at least {args.min_share:.0%} of forecasts still on the trip)")

    names = list(results)
    print("\nmean |forecast median − truth| (ft), and 80% interval coverage, by horizon")
    print(f"{'h (s)':>6} {'n':>6}  " + "  ".join(f"{n[:22]:>22}" for n in names))
    for k, h in enumerate(horizons):
        if h % 60 and h != horizons[-1]:
            continue
        row = [results[n][k] for n in names]
        cells = "  ".join(f"{s.median_abs_ft:9.0f} ({s.coverage:4.2f})".rjust(22) for s in row)
        print(f"{h:6d} {row[0].n:6d}  {cells}")
    print("\nCRPS (ft) by horizon")
    for k, h in enumerate(horizons):
        if h % 60 and h != horizons[-1]:
            continue
        print(f"{h:6d}  " + "  ".join(f"{results[n][k].crps_ft:22.0f}" for n in names))

    if args.csv is not None:
        with args.csv.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["params", "horizon_s", "n", "crps_ft", "median_abs_ft", "median_bias_ft", "coverage"])
            for name, scores in results.items():
                for s in scores:
                    w.writerow([name, s.horizon_s, s.n, s.crps_ft, s.median_abs_ft, s.median_bias_ft, s.coverage])
        print(f"saved the scores to {args.csv}")
    if args.plot is not None:
        plot(results, args.plot, f"Position forecasts, simulated {args.length_mi:g} mi route (seed {args.seed}), "
                                 f"{len(trips)} trips")


if __name__ == "__main__":
    main()
