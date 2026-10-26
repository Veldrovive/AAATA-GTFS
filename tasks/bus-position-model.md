# Bus position model (Phase 2)

Design notes for a generative statistical model of bus positions, fitted by maximum
likelihood. Status: **agreed direction, not implemented**. Referenced from SPEC.md §14.
Decisions here were made in discussion with the user (2026-10-25); change them the same
way.

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

## Order of work

1. SPEC.md §14 items 1–2: clean trip trajectories against fix time.
2. Signal inventory per route from OSM, snapped to shapes; sanity-check against where
   clean trajectories stop away from bus stops.
3. Element sequence per shape (segments, lights, stops, timepoints) and the state space.
4. Forward pass in PyTorch; check it against a brute-force likelihood on a tiny route and
   against simulated data.
5. Fit one route, no covariates; compare simulated and observed distributions (time
   between pings, dwell lengths, time stopped at lights).
6. Covariates and regularisation; then the deferred items if the residuals call for them.
7. Arrival prediction: condition the forward pass on the observations so far and roll
   forward; evaluate on held-out days against the agency baseline.

## Open questions

- Cell size and time step (25 ft / 1 s is a starting guess).
- Phase counts per distribution type, and whether they are fixed or chosen by held-out
  likelihood.
- Train/test split (by day, presumably) and the evaluation metrics for arrival prediction.
- Which weather source to use as a covariate, and at what resolution.
