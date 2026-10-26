"""The model: layout, reference chain, and the torch forward pass against it."""

import numpy as np
import pytest

from aaata_gtfs.model.chain import NO_HOLD, ReferenceChain, State, default_params
from aaata_gtfs.model.data import TripObservations, make_batch
from aaata_gtfs.model.layout import Layout, build_layout
from aaata_gtfs.model.simulate import Error, recovery_errors, simulate_trips, straight_geometry
from aaata_gtfs.osm import LightPosition

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
    """Short trips observed every 3–8 steps (dense, so the tiny route is informative)."""
    chain = ReferenceChain(layout, default_params(layout, n_phases=3, seed=params_seed))
    return simulate_trips(chain, n_trips, np.random.default_rng(seed), cadence=(3, 8), first_hold_s=(0, 19),
                          max_steps=400, linger=20)


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


def test_observed_information_matches_autograd_hessian() -> None:
    torch = pytest.importorskip("torch")
    from aaata_gtfs.model.fit import observed_information
    from aaata_gtfs.model.forward import ForwardModel, Parameters

    layout = tiny_layout()
    trips = simulated_trips(layout, 3, 4, seed=4)
    model = ForwardModel(layout)
    params = Parameters(layout, n_phases=3).set_from(default_params(layout, n_phases=3, seed=3))
    before = torch.nn.utils.parameters_to_vector(params.parameters()).detach().clone()
    info = observed_information(model, params, trips)
    assert torch.equal(torch.nn.utils.parameters_to_vector(params.parameters()).detach(), before)

    # The exact Hessian by double backward.
    leaves = list(params.parameters())
    grads = torch.autograd.grad(model.log_likelihood(params, make_batch(trips)).sum(), leaves, create_graph=True)
    flat = torch.cat([g.reshape(-1) for g in grads])
    rows = [torch.cat([h.reshape(-1) for h in torch.autograd.grad(g, leaves, retain_graph=True)]) for g in flat]
    exact = -torch.stack(rows).detach().numpy()
    assert np.allclose(info, exact, rtol=1e-4, atol=1e-5 * np.abs(exact).max())


def test_forecast_matches_reference() -> None:
    """Position forecasts from every ping equal the dense chain filtered to the
    ping and propagated without observations."""
    pytest.importorskip("torch")
    from aaata_gtfs.model.forward import ForwardModel, Parameters

    layout = tiny_layout()
    params = default_params(layout, n_phases=3, seed=7)
    chain = ReferenceChain(layout, params)
    trips = simulated_trips(layout, 7, 3, seed=8)
    horizons = [0, 5, 17]
    fc = ForwardModel(layout).forecast(Parameters(layout, n_phases=3).set_from(params), make_batch(trips), horizons)
    assert fc.probs.shape == (sum(len(t.steps) for t in trips), 3, layout.n_cells)
    assert np.allclose(fc.probs.sum(-1), 1)

    cells = np.array([chain.cell(s) for s in chain.states])
    for r, (b, n0) in enumerate(zip(fc.trip, fc.origin_step)):
        t = trips[b]
        alpha = chain.initial(t.sched_steps) * chain.obs_likelihood(t.dist_ft[0])
        alpha /= alpha.sum()
        n = 0
        for step, y in zip(t.steps[1:], t.dist_ft[1:]):
            if step > n0:
                break
            while n < step:
                n += 1
                alpha = alpha @ chain.transition_matrix(n, t.sched_steps)
            alpha = alpha * chain.obs_likelihood(y)
            alpha /= alpha.sum()
        for k, h in enumerate(horizons):
            ahead = alpha.copy()
            for m in range(n0 + 1, n0 + h + 1):
                ahead = ahead @ chain.transition_matrix(m, t.sched_steps)
            expected = np.bincount(cells, weights=ahead, minlength=layout.n_cells)
            assert np.allclose(fc.probs[r, k], expected, atol=1e-9)


def test_score_forecasts() -> None:
    from aaata_gtfs.model.simulate import forecast_targets, score_forecasts

    centres = (np.arange(10) + 0.5) * 25.0
    trips = [TripObservations(np.array([0, 4]), np.array([0.0, 60.0]), np.array([NO_HOLD]),
                              true_dist_ft=centres[[0, 0, 1, 2, 3, 9, 9]])]
    horizons = np.array([0, 2, 10])
    truth, scored = forecast_targets(trips, np.array([0, 0]), np.array([0, 4]), horizons, centres[-1])
    # From step 4: on route at +0, at the end at +2 (not scored), past the simulation at +10.
    assert scored.tolist() == [[True, True, False], [True, False, False]]
    assert truth[0, 1] == centres[1]

    # A point mass on the truth scores perfectly; one two cells ahead is 50 ft off.
    probs = np.zeros((2, 3, 10))
    probs[0, 0, 0] = probs[0, 1, 1] = 1.0
    probs[0, 2, 0] = 1.0  # not scored
    probs[1, :, 5] = 1.0
    scores = score_forecasts(probs, centres, truth, scored, horizons)
    assert scores[0].n == 2 and scores[1].n == 1 and scores[2].n == 0
    assert scores[1].median_abs_ft == 0 and scores[1].crps_ft == 0 and scores[1].coverage == 1
    assert scores[0].median_abs_ft == pytest.approx((0 + 50) / 2)
    assert scores[0].crps_ft == pytest.approx((0 + 50) / 2)  # a point forecast's CRPS is its error
    assert scores[0].coverage == 0.5


def test_recovery_errors() -> None:
    layout = tiny_layout()
    true = default_params(layout, n_phases=3, seed=1)
    errors = recovery_errors(true, true, layout)
    assert all(e.rmse == 0 for e in errors.values() if e.n)
    assert Error.of(np.array([1.0, 3.0]), np.array([0.0, 0.0])) == Error(float(np.sqrt(5)), 2.0, 2.0, 2)
