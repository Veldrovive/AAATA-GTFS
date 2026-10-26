"""The bus position model (Phase 2; design in tasks/bus-position-model.md).

- `layout`: a shape cut into cells, with its stops and lights.
- `chain`: the model as a Markov chain, state by state: parameters, the
  reference transition definition, a simulator and a dense exact likelihood.
- `data`: trajectories as observation series and padded batches.
- `forward`: the batched PyTorch forward pass (needs the `model` extra).
- `fit`: maximum likelihood by gradient.

`layout`, `chain` and `data` need only numpy; `forward` and `fit` need torch
(`uv sync --extra model`).
"""

from aaata_gtfs.model.chain import ModelParams, ReferenceChain, default_params, ping_steps
from aaata_gtfs.model.data import ObservationBatch, TripObservations, make_batch, trip_observations
from aaata_gtfs.model.layout import Element, Layout, build_layout

__all__ = [
    "Element",
    "Layout",
    "ModelParams",
    "ObservationBatch",
    "ReferenceChain",
    "TripObservations",
    "build_layout",
    "default_params",
    "make_batch",
    "ping_steps",
    "trip_observations",
]
