"""The forward algorithm in PyTorch: exact log-likelihood, batched over trips.

Implements the chain defined in `chain.py` without building a transition
matrix. The state is kept as four tensors per batch of trips sharing a layout:

- `move`  `[B, n_cells]`
- `light` `[B, n_lights, P]`
- `dwell` `[B, n_stops, P]`
- `hold`  `[B, n_stops]`

A step scatters each cell's mass to where each advance takes it
(`index_add`), splits arrivals at elements, and shifts the phase chains.
Transitions conserve mass, so the forward variables are only normalised at
observations (the scaled forward algorithm), and the log normalisers sum to
the exact `log p(y_1 … y_m | y_0)`.
"""

import math

import numpy as np
import torch
from torch import Tensor, nn

from aaata_gtfs.model.chain import N_PHASES, ModelParams
from aaata_gtfs.model.data import ObservationBatch
from aaata_gtfs.model.layout import Layout

#: Floor on the observation noise (ft), keeping the likelihood bounded.
MIN_SIGMA_FT = 2.0


def _logit(p: np.ndarray | float) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), 1e-9, 1 - 1e-9)
    return np.log(p / (1 - p))


class Parameters(nn.Module):
    """Unconstrained parameters for one layout; `constrained()` maps them to
    probabilities (logistic / softmax) and the noise scale."""

    def __init__(self, layout: Layout, n_phases: int = N_PHASES, dtype: torch.dtype = torch.float64) -> None:
        super().__init__()
        n_lights, n_stops = len(layout.lights), len(layout.stops)

        def zeros(*shape: int) -> nn.Parameter:
            return nn.Parameter(torch.zeros(shape, dtype=dtype))

        self.move_logits = zeros(layout.n_segments, layout.max_advance + 1)
        self.red_logit = zeros(n_lights)
        self.light_pi_logits = zeros(n_lights, n_phases)
        self.light_q_logit = zeros(n_lights)
        self.stop_logit = zeros(n_stops)
        self.dwell_pi_logits = zeros(n_stops, n_phases)
        self.dwell_q_logit = zeros(n_stops)
        self.log_sigma = nn.Parameter(torch.tensor(math.log(20.0), dtype=dtype))
        self.outlier_logit = nn.Parameter(torch.tensor(float(_logit(0.01)), dtype=dtype))

    def constrained(self) -> dict[str, Tensor]:
        return {
            "move_probs": torch.softmax(self.move_logits, -1),
            "p_red": torch.sigmoid(self.red_logit),
            "light_pi": torch.softmax(self.light_pi_logits, -1),
            "light_q": torch.sigmoid(self.light_q_logit),
            "p_stop": torch.sigmoid(self.stop_logit),
            "dwell_pi": torch.softmax(self.dwell_pi_logits, -1),
            "dwell_q": torch.sigmoid(self.dwell_q_logit),
            "sigma_ft": MIN_SIGMA_FT + torch.exp(self.log_sigma),
            "outlier": torch.sigmoid(self.outlier_logit),
        }

    def to_params(self) -> ModelParams:
        c = {k: v.detach().cpu().numpy() for k, v in self.constrained().items()}
        return ModelParams(
            c["move_probs"], c["p_red"], c["light_pi"], c["light_q"], c["p_stop"], c["dwell_pi"], c["dwell_q"],
            float(c["sigma_ft"]), float(c["outlier"]),
        )

    @torch.no_grad()
    def set_from(self, params: ModelParams) -> "Parameters":
        """Set the raw parameters so `constrained()` gives `params`."""
        def put(target: nn.Parameter, value: np.ndarray | float) -> None:
            target.copy_(torch.as_tensor(np.asarray(value, dtype=float), dtype=target.dtype))

        put(self.move_logits, np.log(np.clip(params.move_probs, 1e-12, None)))
        put(self.red_logit, _logit(params.p_red))
        put(self.light_pi_logits, np.log(np.clip(params.light_pi, 1e-12, None)))
        put(self.light_q_logit, _logit(params.light_q))
        put(self.stop_logit, _logit(params.p_stop))
        put(self.dwell_pi_logits, np.log(np.clip(params.dwell_pi, 1e-12, None)))
        put(self.dwell_q_logit, _logit(params.dwell_q))
        put(self.log_sigma, math.log(max(params.sigma_ft - MIN_SIGMA_FT, 1e-6)))
        put(self.outlier_logit, _logit(params.outlier))
        return self


class ForwardModel:
    """The forward pass for one layout (index tables precomputed)."""

    def __init__(self, layout: Layout, device: torch.device | str = "cpu", dtype: torch.dtype = torch.float64) -> None:
        self.layout = layout
        self.device = torch.device(device)
        self.dtype = dtype
        dest, arrival = layout.moves()
        flat_dest, flat_arrival = dest.reshape(-1), arrival.reshape(-1)
        moving = flat_arrival < 0

        def t(a: np.ndarray) -> Tensor:
            return torch.as_tensor(a, dtype=torch.long, device=self.device)

        self.k = layout.max_advance + 1
        self.n_cells = layout.n_cells
        self.segment = t(layout.segment_of_cell())
        self.move_pos, self.move_dest = t(np.flatnonzero(moving)), t(flat_dest[moving])
        self.arrive_pos, self.arrive_elem = t(np.flatnonzero(~moving)), t(flat_arrival[~moving])
        kinds = [e.kind for e in layout.elements]
        self.light_elem = t(np.array([i for i, k in enumerate(kinds) if k == "light"], dtype=int))
        self.stop_elem = t(np.array([i for i, k in enumerate(kinds) if k == "stop"], dtype=int))
        self.light_cells = t(np.array([e.cell for e in layout.lights], dtype=int))
        self.stop_cells = t(np.array([e.cell for e in layout.stops], dtype=int))
        self.n_elements = len(layout.elements)
        centers = layout.cell_centers_ft()
        self.centers = torch.as_tensor(centers, dtype=dtype, device=self.device)
        self.light_centers = self.centers[self.light_cells]
        self.stop_centers = self.centers[self.stop_cells]
        self.length_ft = layout.n_cells * layout.cell_ft
        end = torch.zeros(self.k, dtype=dtype, device=self.device)
        end[0] = 1.0
        self._end_row = end  # the end cell stays put

    def _move_weights(self, p: dict[str, Tensor]) -> Tensor:
        w = p["move_probs"][self.segment]  # [n_cells, k]
        return torch.cat([w[:-1], self._end_row[None]], 0)

    def _obs(self, p: dict[str, Tensor], y: Tensor, centers: Tensor) -> Tensor:
        """p(y | cell centers): `[B, n]` for `y` `[B]`."""
        sigma = p["sigma_ft"]
        z = (y[:, None] - centers[None]) / sigma
        normal = torch.exp(-0.5 * z * z) / (sigma * math.sqrt(2 * math.pi))
        return (1 - p["outlier"]) * normal + p["outlier"] / self.length_ft

    def log_likelihood(self, params: Parameters, batch: ObservationBatch) -> Tensor:
        """`[B]` exact log p(y_1 … y_m | y_0) per trip."""
        p = params.constrained()
        dtype, dev = self.dtype, self.device
        y = torch.as_tensor(batch.y, dtype=dtype, device=dev)
        mask = torch.as_tensor(batch.mask, device=dev)
        sched = torch.as_tensor(batch.sched_steps, dtype=torch.long, device=dev)
        b, n_steps = y.shape[0], y.shape[1] - 1
        n_phases = p["light_pi"].shape[-1]
        n_lights, n_stops = len(self.light_cells), len(self.stop_cells)
        w = self._move_weights(p)
        light_entry = p["p_red"][:, None] * p["light_pi"]  # [n_lights, P]
        dwell_entry = p["p_stop"][:, None] * p["dwell_pi"]
        lq, dq = p["light_q"][:, None], p["dwell_q"][:, None]
        observed_steps = set(np.flatnonzero(batch.mask.any(0)).tolist())

        # Step 0: the prior of `ReferenceChain.initial`, times the first observation.
        early = sched > 0
        move = torch.ones(b, self.n_cells, dtype=dtype, device=dev)
        move = move.index_put((torch.arange(b, device=dev)[:, None], self.stop_cells[None].expand(b, -1)),
                              (~early).to(dtype))
        hold = early.to(dtype)
        light = torch.zeros(b, n_lights, n_phases, dtype=dtype, device=dev)
        dwell = torch.zeros(b, n_stops, n_phases, dtype=dtype, device=dev)
        move, light, dwell, hold, _ = self._observe(p, move, light, dwell, hold, y[:, 0], mask[:, 0])
        total = torch.zeros(b, dtype=dtype, device=dev)

        zeros_phase = torch.zeros(b, 1, dtype=dtype, device=dev)
        for n in range(1, n_steps + 1):
            # Moving: scatter each cell's mass by advance.
            flow = (move[:, :, None] * w[None]).reshape(b, -1)
            new_move = torch.zeros(b, self.n_cells, dtype=dtype, device=dev).index_add(
                1, self.move_dest, flow[:, self.move_pos])
            arrivals = torch.zeros(b, self.n_elements, dtype=dtype, device=dev).index_add(
                1, self.arrive_elem, flow[:, self.arrive_pos])
            at_light = arrivals[:, self.light_elem]
            at_stop = arrivals[:, self.stop_elem]

            # Phase chains: stay with 1 - q, advance with q, leave from the last phase.
            l_adv, d_adv = light * lq, dwell * dq
            new_light = light - l_adv + torch.cat([zeros_phase[:, None].expand(b, n_lights, 1), l_adv[..., :-1]], -1)
            new_dwell = dwell - d_adv + torch.cat([zeros_phase[:, None].expand(b, n_stops, 1), d_adv[..., :-1]], -1)
            new_light = new_light + at_light[:, :, None] * light_entry[None]
            new_dwell = new_dwell + at_stop[:, :, None] * dwell_entry[None]

            # Leaving lights, and stops (held at a timepoint until its scheduled departure).
            new_move = new_move.index_add(1, self.light_cells, l_adv[..., -1] + at_light * (1 - p["p_red"]))
            done = d_adv[..., -1] + at_stop * (1 - p["p_stop"]) + hold
            early = n < sched
            hold = torch.where(early, done, torch.zeros_like(done))
            new_move = new_move.index_add(1, self.stop_cells, torch.where(early, torch.zeros_like(done), done))
            move, light, dwell = new_move, new_light, new_dwell

            if n in observed_steps:
                move, light, dwell, hold, log_c = self._observe(p, move, light, dwell, hold, y[:, n], mask[:, n])
                total = total + log_c
        return total

    def _observe(
        self, p: dict[str, Tensor], move: Tensor, light: Tensor, dwell: Tensor, hold: Tensor, y: Tensor, mask: Tensor,
    ) -> tuple[Tensor, Tensor, Tensor, Tensor, Tensor]:
        """Weight by the observation where `mask`, normalise; returns the log normalisers (0 where unobserved)."""
        y = torch.where(mask, y, torch.zeros_like(y))
        one = torch.ones((), dtype=self.dtype, device=self.device)

        def lik(centers: Tensor) -> Tensor:
            return torch.where(mask[:, None], self._obs(p, y, centers), one)

        move = move * lik(self.centers)
        light = light * lik(self.light_centers)[:, :, None]
        stop_lik = lik(self.stop_centers)
        dwell = dwell * stop_lik[:, :, None]
        hold = hold * stop_lik
        c = move.sum(1) + light.sum((1, 2)) + dwell.sum((1, 2)) + hold.sum(1)
        c = torch.where(mask, c, one)
        inv = (1 / c)[:, None]
        return move * inv, light * inv[:, :, None], dwell * inv[:, :, None], hold * inv, torch.log(c)
