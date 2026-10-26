# Bus position model (Phase 2)

Design notes for a generative statistical model of bus positions, fitted by maximum
likelihood. Status: **first version implemented** (`aaata_gtfs.model`, 2026-10-25): steps
1–4 of the order of work, plus a smoke-test fit (step 5 needs weeks of data). Referenced
from SPEC.md §14. Decisions here were made in discussion with the user (2026-10-25);
change them the same way. The choices made while implementing, within the agreed design,
are under "Implementation (v1)" for review.

## Goal

Model where a bus is, exactly, as a structured generative process along its route, and
fit it to the observed positions. The fun part is the model itself; the technical goal is
arrival-time prediction, evaluated against the agency's own predictions (SPEC.md §14
item 3). Everything should be statistically valid: a real likelihood, real MLE, honest
held-out evaluation.

## The objects

A trip's path (a GTFS shape) is an ordered sequence of three kinds of element at known
distances along the shape (`geometry.ShapeGeometry`):

- **Road segment** — the stretch between two consecutive non-segment elements. Defined by
  a segment-specific distribution of traversal time (parameters θ_seg per segment, not
  shared between segments). Parameterise by pace (time per foot) so sub-segments add up.
- **Traffic light** — from OSM `highway=traffic_signals`, snapped to the shape. Defined
  by P(red) and the distribution of wait time given red. Kept separate (rather than one
  mixed delay distribution) to enforce how signals work: green is a point mass at zero
  delay. For a fixed-time signal, wait | red ≈ Uniform(0, R) and P(red) ≈ R / cycle;
  measured signal timings can seed initial values or a penalty.
- **Stop** — from GTFS. Defined by P(stopping) and the dwell distribution. At a
  **timepoint** (`stop_times.timepoint`) the bus also holds for its scheduled departure:
  departure = max(arrival + dwell, scheduled departure).

Every parameter may depend on covariates (time of day, day type, weather, …) through a
link function: e.g. logit P(red) = α_light + β·x, log mean pace = α_seg + γ·x.

## Observation model

We observe (fix time, position) pairs about every 30 s, already snapped to the street;
project them to distance along the shape. Observed distance = true distance + small
noise, with an outlier component (or a filtering stage before the model) for the AVL
backward jumps. A stopped bus keeps sending new fix times at the same position, so long
dwells and reds are observed directly. Use clean trajectories from SPEC.md §14 items 1–2
(stale trip ids, repeats and jumps handled in filtering stages); exclude detoured trips
at first.

## Inference: discretised HMM, exact likelihood, gradient MLE

- **State:** distance cells along the shape (start with ~25 ft) × mode (moving, waiting
  at light i, dwelling at stop j) × duration phase. Time steps of 1 s.
- **Durations** (segment traversal, red wait, dwell) are **phase-type** distributions
  (Erlang / Coxian chains of exponential phases), which can approximate any positive
  distribution while keeping the state Markov.
- **Likelihood:** the forward algorithm gives the exact marginal likelihood of a trip's
  observations, summing over all latent paths. Most steps are pure propagation; the
  observation term applies only at fix times.
- **Transitions are not a matrix:** almost every transition moves mass one cell forward or
  advances a phase, so a step is a few shifted, weighted copies of α (slice/roll and
  multiply).
- **Numerics:** scaled forward pass (normalise α each step, accumulate log normalisers)
  rather than log space. float64 for the first fits.
- **Fitting:** maximise the summed log-likelihood over trips with gradients from autodiff.
  Trips are independent, so the log-likelihood is a sum over trips and **minibatching
  over trips** gives unbiased gradients (Adam). For the final fit, consider full-batch
  L-BFGS on big hardware, which converges more cleanly for MLE.
- **Regularisation:** add a penalty (ridge-style, i.e. MAP with a weak prior) where
  covariate × element interactions make cells sparse, e.g. one light × rain × 17:00.
  Plain MLE where data are plentiful.
- Forward–backward also gives per-trip posteriors ("probability the bus waited at light
  7"), useful for diagnostics and plots.

## Implementation: PyTorch

- Batch trips that share a shape (same state space) into a `[trips, states]` tensor; one
  Python loop over time steps, each a batched op.
- **Memory:** autograd keeps α for every step of every trip in the batch: one trip is
  roughly 3,600 steps × 5,000 states × 8 B ≈ 144 MB in float64. Control it with the
  minibatch size, plus `torch.utils.checkpoint` over chunks of ~60 steps if needed.
  Fallback if fits get slow: run forward–backward without autograd and use Fisher's
  identity (gradient = expected complete-data gradient under the posteriors), so
  autograd only touches a cheap function of expected counts.
- **Hardware:** prototype on the Pi (CPU) or the M2 Mac; scale to a multi-H100 machine for
  full fits (data-parallel over trip minibatches). **Apple MPS has no float64**: on the
  M2 use CPU for float64, or MPS in float32 once the scaled forward pass is shown to be
  stable.
- Keep the model code independent of the collector; read data through `queries.py` /
  the trip reconstruction stage, read-only.

## Deferred (decided to skip for now)

- **Latent day/traffic effect** shared across elements (or across buses). Ignoring it
  likely makes predictive intervals too narrow. Cheapest later version: one scalar
  slowdown factor per trip on a ~10-point grid, a forward pass per grid point, combined
  with logsumexp. Still exact, 10× compute.
- **Signal coordination** (green waves; red depends on arrival time mod cycle). Check the
  residual correlation between adjacent lights first.
- Identifiability simulations are not a prerequisite (hundreds of traversals per element
  per route), but a small simulator from the same generative model is handy for testing
  the fitting code.

## Implementation (v1)

Code: `trips.py` (clean trajectories), `osm.py` (signals), and `model/`: `layout.py`
(cells and elements), `chain.py` (the chain state by state: the reference definition,
simulator and dense likelihood), `forward.py` (the batched torch forward pass), `data.py`
(trajectories to observation batches), `fit.py` (Adam / L-BFGS). Tests check the torch
forward pass against the dense reference to 1e-9, the simulator against the propagated
chain, and that MLE on simulated trips beats the true parameters' likelihood and recovers
segment speeds and the noise scale. `scripts/fit_shape.py` fits one real shape.

Choices made while implementing (open to change):

- **Movement within a segment:** each 1 s step the bus advances 0…K cells (K = 3, i.e. up
  to 75 ft/s with 25 ft cells) with a categorical distribution per segment (softmax of
  K + 1 logits). Traversal time is then a discrete phase-type distribution, but the steps
  are i.i.d., so there's no persistent speed state: a segment's time varies less than
  real traffic might. Stopping in traffic is the `j = 0` advance. A move that would cross
  an element stops at its cell (the arrival), so up to 1 s is lost at each element.
  Adding a small latent speed regime (free / slow, with persistence) is the obvious
  extension if residuals show overdispersion.
- **Phase-type waits and dwells:** P = 8 phases with one exit probability `q` per element
  and an entry distribution `π` over phases (a mixture of negative binomials with a
  common rate). Minimum wait 1 s.
- **Holds:** every non-final stop can hold; it does if `stop_times.timepoint = 1` and the
  bus is ready before the scheduled departure (`sched_steps`, per trip). A hold state per
  stop releases deterministically at the scheduled step.
- **Elements:** the last stop isn't an element (reaching the end cell ends the trip;
  the end cell is absorbing, so layover pings there are explained). At most one element
  per cell: a collision moves the later one to the next cell, else drops it.
- **Lights** (`osm.py`): `highway=traffic_signals` and signalised crossings
  (`crossing=traffic_signals`), snapped to every pass of the shape within 60 ft and merged
  when within 150 ft along it (OSM maps a signal as ~3 nodes). 2–5 lights per mile on
  the routes checked; a quarter to a third of lights are within 150 ft of a stop.
- **Observation model:** raw projected distance (not the isotonic fit) ~
  `(1 − ε) N(cell centre, σ) + ε U(0, L)`, with σ ≥ 2 ft and ε learnt.
- **Likelihood:** conditional on each trip's first observation, with a uniform prior over
  cells (moving, or held at an early timepoint) at the first fix. So the first ping's
  position carries no information, and the hold at a trip's first stop is only learnt
  from trips observed before departure.
- **Numerics:** float64, normalising only at observation steps (transitions conserve
  mass exactly). On the Pi, one route-4 shape (1,816 cells, 38 lights, 34 stops), 3 trips
  over 2,787 steps: forward 3.1 s, backward 3.6 s, ~1.2 GB peak (≈ 300 MB per trip-hour,
  as estimated). Checkpointing or Fisher's identity aren't needed yet.

## Order of work

1. ✅ SPEC.md §14 items 1–2: clean trip trajectories against fix time.
2. ✅ Signal inventory per route from OSM, snapped to shapes. Still to do, once there's
   more data: sanity-check against where clean trajectories stop away from bus stops.
3. ✅ Element sequence per shape (segments, lights, stops, timepoints) and the state space.
4. ✅ Forward pass in PyTorch; checked against the dense reference likelihood on a tiny
   route and against simulated data.
5. Fit one route, no covariates; compare simulated and observed distributions (time
   between pings, dwell lengths, time stopped at lights). The fitting code and a smoke
   test exist (`scripts/fit_shape.py`); a real fit waits for a few weeks of data.
6. Covariates and regularisation; then the deferred items if the residuals call for them.
7. Arrival prediction: condition the forward pass on the observations so far and roll
   forward; evaluate on held-out days against the agency baseline.

## Open questions

- Cell size and time step (25 ft / 1 s is a starting guess; `CELL_FT`, `MAX_ADVANCE`).
- Phase counts per distribution type, and whether they are fixed or chosen by held-out
  likelihood.
- Train/test split (by day, presumably) and the evaluation metrics for arrival prediction.
- Which weather source to use as a covariate, and at what resolution.
