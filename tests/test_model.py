"""The model: layout, reference chain, and the torch forward pass against it."""

import numpy as np
import pytest

from aaata_gtfs.geometry import ShapeGeometry, ShapeStop
from aaata_gtfs.model.chain import NO_HOLD, ReferenceChain, State, default_params, ping_steps
from aaata_gtfs.model.data import TripObservations, make_batch
from aaata_gtfs.model.layout import Layout, build_layout
from aaata_gtfs.osm import LightPosition

FT_PER_DEG_LAT = 364_000.0


def straight_geometry(length_ft: float, stop_ft: list[float]) -> ShapeGeometry:
    """A north-going straight shape with stops at the given distances."""
    n = 50
    lat0, lon0 = 42.28, -83.74
    points = [(lat0 + length_ft * i / (n - 1) / FT_PER_DEG_LAT, lon0) for i in range(n)]
    stops = [
        ShapeStop(f"s{k}", f"c{k}", None, lat0 + d / FT_PER_DEG_LAT, lon0, k + 1) for k, d in enumerate(stop_ft)
    ]
    return ShapeGeometry("test:straight", "T", points, stops, stop_tolerance_ft=1e9)


def tiny_layout() -> Layout:
    """20 cells of 25 ft: stops at cells 0, 6 and 19 (the last isn't an element), a light at cell 12."""
    geometry = straight_geometry(490.0, [0.0, 160.0, 489.0])
    return build_layout(geometry, [LightPosition(310.0, (1,), "intersection")], cell_ft=25.0, max_advance=2)


def test_layout_cells_and_moves() -> None:
    layout = tiny_layout()
    assert layout.n_cells == 20
    assert [(e.kind, e.cell) for e in layout.elements] == [("stop", 0), ("stop", 6), ("light", 12)]
    assert layout.n_segments == 4
    seg = layout.segment_of_cell()
    assert seg[0] == 1 and seg[5] == 1 and seg[6] == 2 and seg[12] == 3 and seg[19] == 3
    dest, arrival = layout.moves()
    # From cell 5, advancing 2 stops short at the stop in cell 6, as an arrival.
    assert dest[5, 2] == 6 and arrival[5, 2] == 1
    assert dest[4, 2] == 6 and arrival[4, 2] == 1 and arrival[4, 1] == -1
    # Leaving an element's cell is free; the end cell is absorbing.
    assert dest[6, 2] == 8 and arrival[6, 2] == -1
    assert (dest[19] == 19).all()


def test_layout_resolves_collisions() -> None:
    geometry = straight_geometry(490.0, [0.0, 160.0, 489.0])
    layout = build_layout(geometry, [LightPosition(165.0, (1,), "intersection")], cell_ft=25.0)
    assert [(e.kind, e.cell) for e in layout.elements] == [("stop", 0), ("stop", 6), ("light", 7)]


def test_reference_rows_sum_to_one() -> None:
    layout = tiny_layout()
    chain = ReferenceChain(layout, default_params(layout, n_phases=3, seed=1))
    for step in (1, 5, 40):
        m = chain.transition_matrix(step, [10, NO_HOLD])
        np.testing.assert_allclose(m.sum(1), 1.0)


def test_hold_until_scheduled_departure() -> None:
    layout = tiny_layout()
    chain = ReferenceChain(layout, default_params(layout, n_phases=3))
    rng = np.random.default_rng(0)
    for _ in range(20):
        path = chain.simulate(200, [30, NO_HOLD], rng, start=State("hold", 0))
        first_move = next(n for n, s in enumerate(path) if s.kind != "hold")
        assert first_move == 30


def simulated_trips(layout: Layout, params_seed: int, n_trips: int, seed: int) -> list[TripObservations]:
    chain = ReferenceChain(layout, default_params(layout, n_phases=3, seed=params_seed))
    rng = np.random.default_rng(seed)
    trips = []
    for _ in range(n_trips):
        sched = np.array([int(rng.integers(0, 20)), NO_HOLD])
        path = chain.simulate(400, sched, rng, start=State("hold", 0) if sched[0] > 0 else State("move", 0), linger=20)
        steps = ping_steps(len(path) - 1, rng, low=3, high=8)
        trips.append(TripObservations(np.array(steps), chain.observe(path, steps, rng), sched))
    return trips


def test_forward_matches_reference() -> None:
    torch = pytest.importorskip("torch")
    from aaata_gtfs.model.forward import ForwardModel, Parameters

    layout = tiny_layout()
    params = default_params(layout, n_phases=3, seed=7)
    chain = ReferenceChain(layout, params)
    trips = simulated_trips(layout, 7, 4, seed=3)
    expected = [chain.log_likelihood(t.steps, t.dist_ft, t.sched_steps) for t in trips]

    model = ForwardModel(layout)
    theta = Parameters(layout, n_phases=3).set_from(params)
    got = model.log_likelihood(theta, make_batch(trips))
    np.testing.assert_allclose(got.detach().numpy(), expected, rtol=1e-9)

    # And it's differentiable.
    got.sum().backward()
    assert theta.move_logits.grad is not None and torch.isfinite(theta.move_logits.grad).all()
    assert theta.dwell_q_logit.grad is not None and theta.dwell_q_logit.grad.abs().sum() > 0


def test_forward_marginals_match_simulation() -> None:
    """The chain propagated without observations matches the simulator's state frequencies."""
    layout = tiny_layout()
    chain = ReferenceChain(layout, default_params(layout, n_phases=3, seed=2))
    sched = [15, NO_HOLD]
    alpha = np.zeros(len(chain.states))
    alpha[chain.index[State("hold", 0)]] = 1.0
    for n in range(1, 61):
        alpha = alpha @ chain.transition_matrix(n, sched)
    rng = np.random.default_rng(5)
    n_sims = 3000
    counts = np.zeros(len(chain.states))
    for _ in range(n_sims):
        counts[chain.index[chain.simulate(60, sched, rng, start=State("hold", 0))[-1]]] += 1
    # Every state's frequency within 4.5 standard errors of its probability.
    se = np.sqrt(alpha * (1 - alpha) / n_sims) + 1e-3
    assert (np.abs(counts / n_sims - alpha) < 4.5 * se).all()


def test_fit_recovers_simulated_parameters() -> None:
    """MLE on simulated trips beats the true parameters' likelihood and lands near them."""
    pytest.importorskip("torch")
    from aaata_gtfs.model.fit import fit_lbfgs, mean_log_likelihood
    from aaata_gtfs.model.forward import ForwardModel, Parameters

    layout = tiny_layout()
    true = default_params(layout, n_phases=3, seed=11)
    trips = simulated_trips(layout, 11, 40, seed=12)
    model = ForwardModel(layout)
    truth_ll = mean_log_likelihood(model, Parameters(layout, n_phases=3).set_from(true), trips)

    theta = Parameters(layout, n_phases=3).set_from(default_params(layout, n_phases=3))
    start_ll = mean_log_likelihood(model, theta, trips)
    fit_lbfgs(model, theta, trips, max_iter=60)
    fitted_ll = mean_log_likelihood(model, theta, trips)
    assert start_ll < truth_ll <= fitted_ll + 1e-6

    fitted = theta.to_params()
    # Mean advance (cells per step) on the long segments, and the noise scale.
    advance = np.arange(layout.max_advance + 1)
    for seg in (1, 3):
        assert abs(fitted.move_probs[seg] @ advance - true.move_probs[seg] @ advance) < 0.15
    assert abs(fitted.sigma_ft - true.sigma_ft) < 5
