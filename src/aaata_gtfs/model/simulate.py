"""Simulated data from the model, and how well a fit recovers the parameters.

Used to check the fitting code: simulate trips from known parameters at the
feed's cadence, fit, and compare the fitted values with the truth in
physical units (segment speeds in mph, waits and dwells in seconds).
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from aaata_gtfs.geometry import ShapeGeometry, ShapeStop
from aaata_gtfs.model.chain import NO_HOLD, ModelParams, ReferenceChain, State, ping_steps
from aaata_gtfs.model.data import TripObservations
from aaata_gtfs.model.layout import CELL_FT, MAX_ADVANCE, Layout, build_layout
from aaata_gtfs.osm import LightPosition

FT_PER_DEG_LAT = 364_000.0
FTPS_PER_MPH = 5280 / 3600


def straight_geometry(length_ft: float, stop_ft: Sequence[float], key: str = "sim:straight") -> ShapeGeometry:
    """A north-going straight shape with stops at the given distances."""
    n = 50
    lat0, lon0 = 42.28, -83.74
    points = [(lat0 + length_ft * i / (n - 1) / FT_PER_DEG_LAT, lon0) for i in range(n)]
    stops = [
        ShapeStop(f"s{k}", f"c{k}", None, lat0 + d / FT_PER_DEG_LAT, lon0, k + 1) for k, d in enumerate(stop_ft)
    ]
    return ShapeGeometry(key, "SIM", points, stops, stop_tolerance_ft=1e9)


def synthetic_layout(
    rng: np.random.Generator,
    length_ft: float = 10_560.0,
    stop_spacing_ft: float = 1_300.0,
    light_spacing_ft: float = 1_800.0,
    cell_ft: float = CELL_FT,
    max_advance: int = MAX_ADVANCE,
) -> Layout:
    """A straight route with roughly evenly spaced (jittered) stops and lights."""
    n_stops = max(int(length_ft // stop_spacing_ft), 1)
    stops = [0.0] + sorted(rng.uniform(0.85, 1.15, n_stops - 1) * stop_spacing_ft * np.arange(1, n_stops)) + [
        length_ft - 1
    ]
    n_lights = max(int(length_ft // light_spacing_ft), 1)
    lights = [
        LightPosition(float(d), (k,), "intersection")
        for k, d in enumerate(rng.uniform(0.2, 0.8, n_lights) * light_spacing_ft + light_spacing_ft * np.arange(n_lights))
        if d < length_ft - 2 * cell_ft
    ]
    return build_layout(straight_geometry(length_ft, stops), lights, cell_ft, max_advance)


def _advance_probs(speed_mph: float, layout: Layout, p_halt: float) -> np.ndarray:
    """Advance probabilities with about the given mean speed: `p_halt` at 0, the
    rest a discretised normal around the matching advance."""
    a = speed_mph * FTPS_PER_MPH / layout.cell_ft
    j = np.arange(layout.max_advance + 1)
    w = np.exp(-0.5 * ((j - a) / 0.7) ** 2)
    w[0] = 0.0
    return np.concatenate([[p_halt], (1 - p_halt) * w[1:] / w[1:].sum()])


def _phase_type(rng: np.random.Generator, n: int, n_phases: int, low_s: float, high_s: float) -> tuple[np.ndarray, np.ndarray]:
    """(pi, q) for `n` waits with random entry mixtures and means in [low_s, high_s]."""
    pi = rng.dirichlet(np.ones(n_phases), n) if n else np.zeros((0, n_phases))
    stages = np.arange(n_phases, 0, -1)
    q = np.clip(pi @ stages / rng.uniform(low_s, high_s, n), 0.02, 0.95)
    return pi, q


def random_params(
    layout: Layout,
    rng: np.random.Generator,
    n_phases: int,
    speed_mph: tuple[float, float] = (8.0, 40.0),
    p_red: tuple[float, float] = (0.2, 0.7),
    red_wait_s: tuple[float, float] = (15.0, 60.0),
    p_stop: tuple[float, float] = (0.2, 0.9),
    dwell_s: tuple[float, float] = (8.0, 40.0),
    sigma_ft: float = 20.0,
    outlier: float = 0.01,
) -> ModelParams:
    """Physically varied true parameters for a recovery study: each element's
    value drawn uniformly from the given range."""
    n_lights, n_stops = len(layout.lights), len(layout.stops)
    move = np.stack([
        _advance_probs(rng.uniform(*speed_mph), layout, rng.uniform(0.03, 0.15)) for _ in range(layout.n_segments)
    ])
    light_pi, light_q = _phase_type(rng, n_lights, n_phases, *red_wait_s)
    dwell_pi, dwell_q = _phase_type(rng, n_stops, n_phases, *dwell_s)
    return ModelParams(
        move, rng.uniform(*p_red, n_lights), light_pi, light_q, rng.uniform(*p_stop, n_stops), dwell_pi, dwell_q,
        sigma_ft, outlier,
    )


def simulate_trips(
    chain: ReferenceChain,
    n_trips: int,
    rng: np.random.Generator,
    cadence: tuple[int, int] = (25, 35),
    first_hold_s: tuple[int, int] = (0, 120),
    timepoints: Sequence[int] = (),
    max_steps: int = 20_000,
    linger: int = 60,
) -> list[TripObservations]:
    """Trips from the start of the layout, observed every `cadence` steps
    until `linger` steps after they reach the end.

    The first stop holds the bus for `first_hold_s` (uniform). Each stop index
    in `timepoints` also holds, until a scheduled departure drawn from 0.8–1.1×
    the time a bus with no holds first leaves it (so roughly half the trips
    run early enough to be held).
    """
    no_holds = np.full(len(chain.layout.stops), NO_HOLD)
    nominal: dict[int, int] = {}
    if timepoints:
        pilots = [chain.simulate(max_steps, no_holds, rng, linger=1) for _ in range(20)]
        for j in timepoints:
            cell = chain.stop_cells[j]
            nominal[j] = int(np.median([
                next(n for n, s in enumerate(path) if s.kind == "move" and s.index >= cell + 1) for path in pilots
            ]))
    trips = []
    for _ in range(n_trips):
        sched = no_holds.copy()
        sched[0] = int(rng.integers(first_hold_s[0], first_hold_s[1] + 1))
        for j, n in nominal.items():
            sched[j] = int(n * rng.uniform(0.8, 1.1))
        start = State("hold", 0) if sched[0] > 0 else State("move", 0)
        path = chain.simulate(max_steps, sched, rng, start=start, linger=linger)
        steps = ping_steps(len(path) - 1, rng, *cadence)
        true_ft = (np.array([chain.cell(s) for s in path]) + 0.5) * chain.layout.cell_ft
        trips.append(TripObservations(np.array(steps), chain.observe(path, steps, rng), sched, true_dist_ft=true_ft))
    return trips


# ------------------------------------------------------------------ recovery


def segment_speeds_mph(params: ModelParams, layout: Layout) -> np.ndarray:
    """Mean moving speed on each segment (mph): mean advance per 1 s step."""
    advance_ft = np.arange(layout.max_advance + 1) * layout.cell_ft
    return params.move_probs @ advance_ft / FTPS_PER_MPH


def segment_lengths_cells(layout: Layout) -> np.ndarray:
    """Cells per segment (excluding the end cell)."""
    return np.bincount(layout.segment_of_cell()[:-1], minlength=layout.n_segments)


def mean_phase_duration_s(pi: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Mean of each phase-type wait: entering phase k leaves P - k stages of mean 1/q."""
    stages = np.arange(pi.shape[1], 0, -1)
    return pi @ stages / q


@dataclass(frozen=True)
class Error:
    """How far fitted values are from the truth, over a group of parameters."""

    rmse: float
    bias: float  # mean of fitted - true
    mae: float
    n: int

    @staticmethod
    def of(fitted: np.ndarray, true: np.ndarray, weights: np.ndarray | None = None) -> "Error":
        d = np.asarray(fitted, dtype=float) - np.asarray(true, dtype=float)
        w = np.ones_like(d) if weights is None else np.asarray(weights, dtype=float)
        if not len(d) or w.sum() == 0:
            return Error(float("nan"), float("nan"), float("nan"), 0)
        w = w / w.sum()
        return Error(float(np.sqrt(w @ d**2)), float(w @ d), float(w @ np.abs(d)), len(d))


def recovery_errors(fitted: ModelParams, true: ModelParams, layout: Layout) -> dict[str, Error]:
    """Errors in physical units, by parameter group."""
    lengths = segment_lengths_cells(layout)
    used = lengths > 0
    return {
        "speed_mph": Error.of(segment_speeds_mph(fitted, layout)[used], segment_speeds_mph(true, layout)[used]),
        "speed_mph_length_weighted": Error.of(
            segment_speeds_mph(fitted, layout)[used], segment_speeds_mph(true, layout)[used], lengths[used]
        ),
        "p_red": Error.of(fitted.p_red, true.p_red),
        "red_wait_s": Error.of(
            mean_phase_duration_s(fitted.light_pi, fitted.light_q), mean_phase_duration_s(true.light_pi, true.light_q)
        ),
        "p_stop": Error.of(fitted.p_stop[1:], true.p_stop[1:]),  # the first stop is never arrived at
        "dwell_s": Error.of(
            mean_phase_duration_s(fitted.dwell_pi, fitted.dwell_q)[1:],
            mean_phase_duration_s(true.dwell_pi, true.dwell_q)[1:],
        ),
        # Expected delay per pass, p × mean wait: what 30 s observations constrain
        # best, even when the split between the probability and the wait is loose.
        "light_delay_s": Error.of(
            fitted.p_red * mean_phase_duration_s(fitted.light_pi, fitted.light_q),
            true.p_red * mean_phase_duration_s(true.light_pi, true.light_q),
        ),
        "stop_delay_s": Error.of(
            (fitted.p_stop * mean_phase_duration_s(fitted.dwell_pi, fitted.dwell_q))[1:],
            (true.p_stop * mean_phase_duration_s(true.dwell_pi, true.dwell_q))[1:],
        ),
        "sigma_ft": Error.of(np.array([fitted.sigma_ft]), np.array([true.sigma_ft])),
        "outlier": Error.of(np.array([fitted.outlier]), np.array([true.outlier])),
    }


def study_setup(seed: int, length_mi: float, n_phases: int) -> tuple[Layout, ModelParams, list[int], np.random.Generator]:
    """`scripts/simulation_study.py`'s route, true parameters and timepoints for
    a seed, and its generator positioned to draw the training trips next."""
    rng = np.random.default_rng(seed)
    layout = synthetic_layout(rng, length_ft=length_mi * 5280)
    true = random_params(layout, rng, n_phases)
    return layout, true, [len(layout.stops) // 2], rng


# ------------------------------------------------------------------ forecasts


@dataclass(frozen=True)
class HorizonScore:
    """How good position forecasts `horizon_s` ahead are, against the true
    (noise-free) position, over the forecasts scored at that horizon."""

    horizon_s: int
    n: int
    crps_ft: float  # mean continuous ranked probability score
    median_abs_ft: float  # mean |forecast median - truth|
    median_bias_ft: float  # mean forecast median - truth
    coverage: float  # share of truths inside the central `level` interval


def forecast_targets(
    trips: Sequence[TripObservations], trip: np.ndarray, origin_step: np.ndarray, horizons: np.ndarray, end_ft: float,
) -> tuple[np.ndarray, np.ndarray]:
    """The true distance `[R, H]` at each forecast's origin + horizon, and
    whether it's scored: the bus hasn't reached the end of the route by then
    (at or past it, where it is is trivial), and the simulation ran that long."""
    truth = np.full((len(trip), len(horizons)), np.nan)
    for r, (b, n) in enumerate(zip(trip, origin_step)):
        true_ft = trips[b].true_dist_ft
        if true_ft is None:
            raise ValueError("forecasts can only be scored on simulated trips (true_dist_ft)")
        steps = n + horizons
        ok = steps < len(true_ft)
        truth[r, ok] = true_ft[steps[ok]]
    return truth, np.isfinite(truth) & (truth < end_ft)


def score_forecasts(
    probs: np.ndarray, centres_ft: np.ndarray, truth: np.ndarray, scored: np.ndarray, horizons: np.ndarray,
    level: float = 0.8,
) -> list[HorizonScore]:
    """Scores per horizon for distributions over cells `probs` `[R, H, n_cells]`
    (cell centres `centres_ft`) against `truth` `[R, H]` where `scored`."""
    cdf = np.cumsum(probs, -1)
    cdf /= cdf[..., -1:]
    width = np.diff(centres_ft, append=centres_ft[-1] + (centres_ft[-1] - centres_ft[-2]))

    def quantile(q: float) -> np.ndarray:
        return centres_ft[np.minimum((cdf < q).sum(-1), len(centres_ft) - 1)]

    median, low, high = quantile(0.5), quantile((1 - level) / 2), quantile((1 + level) / 2)
    out = []
    for k, h in enumerate(horizons):
        m = scored[:, k]
        if not m.any():
            out.append(HorizonScore(int(h), 0, *[float("nan")] * 4))
            continue
        t = truth[m, k]
        step = (centres_ft[None] >= t[:, None]).astype(float)
        crps = ((cdf[m, k] - step) ** 2 * width[None]).sum(-1)
        err = median[m, k] - t
        inside = (low[m, k] <= t) & (t <= high[m, k])
        out.append(HorizonScore(int(h), int(m.sum()), float(crps.mean()), float(np.abs(err).mean()),
                                float(err.mean()), float(inside.mean())))
    return out
