"""Maximum likelihood by gradient: Adam over trip minibatches, or full-batch L-BFGS.

Trips are independent, so the log-likelihood is a sum over trips and a
minibatch's mean gives an unbiased gradient. The objective is the mean
log-likelihood per observation (excluding each trip's first, which is
conditioned on), so its scale doesn't depend on the data size.
"""

import logging
from collections.abc import Sequence

import numpy as np
import torch

from aaata_gtfs.model.data import TripObservations, make_batch
from aaata_gtfs.model.forward import ForwardModel, Parameters

log = logging.getLogger(__name__)


def n_scored(trips: Sequence[TripObservations]) -> int:
    """Observations that contribute to the likelihood (all but each trip's first)."""
    return sum(len(t.steps) - 1 for t in trips)


def mean_log_likelihood(model: ForwardModel, params: Parameters, trips: Sequence[TripObservations]) -> float:
    with torch.no_grad():
        return float(model.log_likelihood(params, make_batch(trips)).sum()) / max(n_scored(trips), 1)


def fit_adam(
    model: ForwardModel,
    params: Parameters,
    trips: Sequence[TripObservations],
    n_iter: int = 200,
    lr: float = 0.05,
    batch_size: int | None = None,
    seed: int = 0,
    log_every: int = 10,
) -> list[float]:
    """Adam on minibatches of trips (all of them if `batch_size` is None).
    Returns the minibatch mean log-likelihood per observation at each iteration."""
    rng = np.random.default_rng(seed)
    optimizer = torch.optim.Adam(params.parameters(), lr=lr)
    history = []
    for it in range(n_iter):
        if batch_size is None or batch_size >= len(trips):
            chosen = list(trips)
        else:
            chosen = [trips[i] for i in rng.choice(len(trips), batch_size, replace=False)]
        optimizer.zero_grad()
        loss = -model.log_likelihood(params, make_batch(chosen)).sum() / max(n_scored(chosen), 1)
        loss.backward()
        optimizer.step()
        history.append(-loss.item())
        if log_every and it % log_every == 0:
            log.info("iter %d: mean log-lik %.4f", it, history[-1])
    return history


def fit_lbfgs(
    model: ForwardModel,
    params: Parameters,
    trips: Sequence[TripObservations],
    max_iter: int = 100,
    lr: float = 1.0,
) -> list[float]:
    """Full-batch L-BFGS (strong Wolfe line search). Returns the mean
    log-likelihood per observation at each evaluation."""
    batch = make_batch(trips)
    scale = max(n_scored(trips), 1)
    optimizer = torch.optim.LBFGS(params.parameters(), lr=lr, max_iter=max_iter, line_search_fn="strong_wolfe")
    history: list[float] = []

    def closure() -> torch.Tensor:
        optimizer.zero_grad()
        loss = -model.log_likelihood(params, batch).sum() / scale
        loss.backward()
        history.append(-loss.item())
        return loss

    optimizer.step(closure)  # pyrefly: ignore[bad-argument-type]
    return history
