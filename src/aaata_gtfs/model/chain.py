"""The bus position model as a Markov chain, state by state (the reference).

One step is one second. The latent state is one of:

- `State("move", c)`: in cell `c`, free to move. Each step the bus advances `j`
  cells (`j = 0 … max_advance`) with probabilities that depend on the road
  segment it's on, but never past the next element: a move that would cross
  one stops at its cell, as an *arrival*. The end cell is absorbing.
- `State("light", i, k)`: waiting at red light `i`, in phase `k` of its wait.
- `State("dwell", j, k)`: dwelling at stop `j`, in phase `k`.
- `State("hold", j)`: held at timepoint stop `j` until its scheduled departure.

Arriving at light `i`: red with probability `p_red[i]` (wait), else pass on.
Arriving at stop `j`: dwell with probability `p_stop[j]`, else pass. After a
dwell or pass, a bus at a timepoint before its scheduled departure holds
until that step, then moves on (departure = max(arrival + dwell, schedule)).

Waits and dwells are discrete phase-type: a chain of `P` phases, entered at
phase `k` with probability `pi[k]`, each phase left (to the next, or out of
the last) with probability `q` per step. Entering at phase `k` gives a
negative-binomial duration (`P - k` geometric stages); the mixture over entry
phases approximates any positive duration as `P` grows.

Observations are distances along the shape at some steps:
`y ~ (1 - eps) Normal(center of the state's cell, sigma) + eps Uniform(0, L)`.

This module is the readable definition: `successors` lists each state's next
states, and the simulator and the dense likelihood are built on it. The
vectorised forward pass in `forward.py` must agree with it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np

from aaata_gtfs.model.layout import Layout

#: Phases per wait / dwell distribution.
N_PHASES = 8



class State(NamedTuple):
    kind: str  # "move", "light", "dwell" or "hold"
    index: int  # the cell (move), light (light) or stop (dwell, hold)
    phase: int = 0


#: Steps or schedule steps: a sequence or a numpy int array.
Ints = Sequence[int] | np.ndarray

#: `sched_steps` value for a stop with no hold (not a timepoint).
NO_HOLD = -1


@dataclass(frozen=True)
class ModelParams:
    """Constrained parameters for one layout, as numpy arrays."""

    move_probs: np.ndarray  # [n_segments, max_advance + 1], rows sum to 1
    p_red: np.ndarray  # [n_lights]
    light_pi: np.ndarray  # [n_lights, P], rows sum to 1
    light_q: np.ndarray  # [n_lights]
    p_stop: np.ndarray  # [n_stops]
    dwell_pi: np.ndarray  # [n_stops, P]
    dwell_q: np.ndarray  # [n_stops]
    sigma_ft: float
    outlier: float

    @property
    def n_phases(self) -> int:
        return self.light_pi.shape[1]


def default_params(layout: Layout, n_phases: int = N_PHASES, seed: int | None = None) -> ModelParams:
    """Plausible parameters (randomised if `seed` is given), e.g. for simulation."""
    rng = np.random.default_rng(seed)
    k = layout.max_advance + 1
    jitter = (lambda n: rng.uniform(0.7, 1.3, n)) if seed is not None else (lambda n: np.ones(n))
    move = np.tile(np.array([1.0, 2.0, 4.0, 2.0][:k] + [1.0] * max(0, k - 4)), (layout.n_segments, 1))
    move = move * jitter(move.size).reshape(move.shape)
    move /= move.sum(1, keepdims=True)
    n_lights, n_stops = len(layout.lights), len(layout.stops)
    pi = np.full(n_phases, 1.0 / n_phases)
    return ModelParams(
        move_probs=move,
        p_red=np.clip(0.4 * jitter(n_lights), 0.05, 0.95),
        light_pi=np.tile(pi, (n_lights, 1)),
        light_q=np.clip(0.15 * jitter(n_lights), 0.02, 0.9),
        p_stop=np.clip(0.5 * jitter(n_stops), 0.05, 0.95),
        dwell_pi=np.tile(pi, (n_stops, 1)),
        dwell_q=np.clip(0.3 * jitter(n_stops), 0.02, 0.9),
        sigma_ft=20.0,
        outlier=0.01,
    )


class ReferenceChain:
    """The chain on one layout, state by state. Small layouts only."""

    def __init__(self, layout: Layout, params: ModelParams) -> None:
        self.layout = layout
        self.params = params
        self.P = params.n_phases
        self.light_index = {e.cell: i for i, e in enumerate(layout.lights)}
        self.stop_index = {e.cell: j for j, e in enumerate(layout.stops)}
        self.light_cells = [e.cell for e in layout.lights]
        self.stop_cells = [e.cell for e in layout.stops]
        self.segment = layout.segment_of_cell()
        self.dest, self.arrival = layout.moves()
        self.states: list[State] = (
            [State("move", c) for c in range(layout.n_cells)]
            + [State("light", i, k) for i in range(len(self.light_cells)) for k in range(self.P)]
            + [State("dwell", j, k) for j in range(len(self.stop_cells)) for k in range(self.P)]
            + [State("hold", j) for j in range(len(self.stop_cells))]
        )
        self.index = {s: n for n, s in enumerate(self.states)}

    def cell(self, state: State) -> int:
        if state.kind == "move":
            return state.index
        if state.kind == "light":
            return self.light_cells[state.index]
        return self.stop_cells[state.index]

    def _leave_stop(self, j: int, step: int, sched_steps: Ints) -> State:
        """Where a bus done dwelling (or passing) at stop `j` is at `step`."""
        return State("hold", j) if step < sched_steps[j] else State("move", self.stop_cells[j])

    def successors(self, state: State, step: int, sched_steps: Ints) -> list[tuple[State, float]]:
        """`(next state, probability)` for the step into `step` (the new step's index)."""
        p = self.params
        out: list[tuple[State, float]] = []
        kind = state.kind
        if kind == "move":
            c = state.index
            if c == self.layout.end_cell:
                return [(state, 1.0)]
            probs = p.move_probs[self.segment[c]]
            for j, pj in enumerate(probs):
                d, e = int(self.dest[c, j]), int(self.arrival[c, j])
                if e < 0:
                    out.append((State("move", d), pj))
                elif d in self.light_index:
                    i = self.light_index[d]
                    out.append((State("move", d), pj * (1 - p.p_red[i])))
                    out += [(State("light", i, k), pj * p.p_red[i] * p.light_pi[i, k]) for k in range(self.P)]
                else:
                    js = self.stop_index[d]
                    out.append((self._leave_stop(js, step, sched_steps), pj * (1 - p.p_stop[js])))
                    out += [(State("dwell", js, k), pj * p.p_stop[js] * p.dwell_pi[js, k]) for k in range(self.P)]
        elif kind == "light":
            i, k = state.index, state.phase
            q = p.light_q[i]
            out.append((state, 1 - q))
            out.append((State("light", i, k + 1) if k + 1 < self.P else State("move", self.light_cells[i]), q))
        elif kind == "dwell":
            j, k = state.index, state.phase
            q = p.dwell_q[j]
            out.append((state, 1 - q))
            out.append((State("dwell", j, k + 1) if k + 1 < self.P else self._leave_stop(j, step, sched_steps), q))
        else:  # hold
            out.append((self._leave_stop(state.index, step, sched_steps), 1.0))
        # Merge duplicates (e.g. several advances stopped short at one cell).
        merged: dict[State, float] = {}
        for s, prob in out:
            merged[s] = merged.get(s, 0.0) + float(prob)
        return list(merged.items())

    def initial(self, sched_steps: Ints) -> np.ndarray:
        """The prior over states at step 0: every cell equally likely, free to
        move, except that a timepoint stop's cell is held if it's early."""
        alpha = np.zeros(len(self.states))
        for c in range(self.layout.n_cells):
            j = self.stop_index.get(c)
            state = self._leave_stop(j, 0, sched_steps) if j is not None else State("move", c)
            alpha[self.index[state]] += 1.0
        return alpha / alpha.sum()

    def transition_matrix(self, step: int, sched_steps: Ints) -> np.ndarray:
        """Dense `[from, to]` matrix for the step into `step`."""
        n = len(self.states)
        m = np.zeros((n, n))
        for a, s in enumerate(self.states):
            for t, prob in self.successors(s, step, sched_steps):
                m[a, self.index[t]] += prob
        return m

    def obs_likelihood(self, y: float) -> np.ndarray:
        """p(y | state) for every state."""
        p = self.params
        centers = (np.array([self.cell(s) for s in self.states]) + 0.5) * self.layout.cell_ft
        z = (y - centers) / p.sigma_ft
        normal = np.exp(-0.5 * z * z) / (p.sigma_ft * np.sqrt(2 * np.pi))
        return (1 - p.outlier) * normal + p.outlier / (self.layout.n_cells * self.layout.cell_ft)

    def log_likelihood(self, steps: Ints, ys: Sequence[float] | np.ndarray, sched_steps: Ints) -> float:
        """Exact log p(y_1 … y_m | y_0) by the dense forward algorithm.

        `steps` are the observation steps (the first is 0), increasing."""
        alpha = self.initial(sched_steps) * self.obs_likelihood(ys[0])
        alpha /= alpha.sum()
        total = 0.0
        n = 0
        for step, y in zip(steps[1:], ys[1:]):
            while n < step:
                n += 1
                alpha = alpha @ self.transition_matrix(n, sched_steps)
            alpha = alpha * self.obs_likelihood(y)
            c = alpha.sum()
            total += float(np.log(c))
            alpha /= c
        return total

    # --------------------------------------------------------------- simulation

    def simulate(
        self,
        n_steps: int,
        sched_steps: Ints,
        rng: np.random.Generator,
        start: State = State("move", 0),
        linger: int | None = None,
    ) -> list[State]:
        """A latent path from `start` (step 0): `n_steps + 1` states, or fewer
        if `linger` is given and the bus has been at the end cell that many steps."""
        path = [start]
        end = State("move", self.layout.end_cell)
        at_end = 0
        for n in range(1, n_steps + 1):
            nexts = self.successors(path[-1], n, sched_steps)
            probs = np.array([p for _, p in nexts])
            path.append(nexts[rng.choice(len(nexts), p=probs / probs.sum())][0])
            at_end = at_end + 1 if path[-1] == end else 0
            if linger is not None and at_end >= linger:
                break
        return path

    def observe(self, path: Sequence[State], steps: Ints, rng: np.random.Generator) -> np.ndarray:
        """Noisy distances of a latent path at the given steps."""
        p = self.params
        length = self.layout.n_cells * self.layout.cell_ft
        ys = []
        for n in steps:
            if rng.random() < p.outlier:
                ys.append(rng.uniform(0, length))
            else:
                ys.append(rng.normal((self.cell(path[n]) + 0.5) * self.layout.cell_ft, p.sigma_ft))
        return np.array(ys)


def ping_steps(n_steps: int, rng: np.random.Generator, low: int = 25, high: int = 35) -> list[int]:
    """Observation steps 0, then every `low`–`high` steps (the feed's cadence)."""
    steps = [0]
    while (nxt := steps[-1] + int(rng.integers(low, high + 1))) <= n_steps:
        steps.append(nxt)
    return steps

