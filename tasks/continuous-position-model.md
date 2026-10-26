# Continuous-position bus model (V1)

A second version of the bus position model in which the bus moves through continuous
space and segment parameters describe continuous speed distributions. Status:
**planned** (written 2026-10-26 from a discussion with the user). Background and the
current discrete model: [bus-position-model.md](bus-position-model.md). This note covers
**V1 only**; once it is fitted and evaluated, decide from its residuals what (if
anything) comes next.

## Why: what the discrete model gets wrong

In the current model (`model/chain.py`, `model/forward.py`) the bus advances
`j ∈ {0 … 3}` whole 25 ft cells each 1 s step, drawn independently from a categorical
distribution per segment.

1. **The mean and variance of the speed are tied together.** A bus at 10 ft/s needs
   E[j] = 0.4, so P(j = 0) ≥ 0.6. The time spent in each cell is then geometric (mean
   2.5 s, SD 1.9 s, most likely 1 s), far more spread than a bus moving steadily. Over a
   1,000 ft segment that is an SD of ~12 s from step noise alone. "Moving 0" ends up
   standing in for "moving slowly", so slow cells carry spurious stopped mass.
2. **Stop-and-go traffic can only be represented by independent seconds:** a large
   `p₀` plus mass on fast advances. The fit gets the mean right but not the structure,
   and long stops away from elements don't come out of the chain.
3. **The forecast median lags.** The extra spread interacts with elements: the leading
   tail piles up at the next light or stop and pays its wait, while the trailing tail
   spreads back freely. (This is a hypothesis; V1's evaluation should check it.)
4. **Element artefacts:** a move that would cross an element stops at its cell, even
   when the bus passes it (green light, skipped stop), so each element costs a forced
   step plus the leftover distance. Elements are rounded to cells, with collisions
   moved or dropped (`build_layout`). Observations are compared with cell centres, so
   rounding adds ~25/√12 ≈ 7 ft of SD that σ has to absorb.

Phases within cells (Erlang instead of geometric times) would fix 1 within the discrete
model, but they cap variance reduction at "phases ≤ seconds per cell" and still round
positions. A continuous model fixes 1, 3 (partly) and 4 directly.

## The V1 model

The bus's position x is continuous in [0, L] ft along the shape. The time step is still
1 s. The latent state is a continuous position plus a discrete mode:

- **Moving at x.** Each step the bus advances `D` ft, with
  `log D ~ Normal(μ_c, σ_c)` (log-normal), where c is the **parameter cell** containing
  x. Speed is **drawn fresh every second**: there is no persistent speed state in V1.
  Spread after t seconds grows like √t · SD(D), but the mean speed and its spread are
  separate parameters. (At 10 ft/s with a 30% coefficient of variation: ~16 ft SD over
  30 s, instead of the discrete model's ~65 ft.)
- **Queued at x** (stopped in traffic away from an element). A log-normal step can't be
  zero, so stopping needs its own mode. From moving, a step enters the queue with
  probability `a_c` (the bus doesn't move that step). From queued, each step the bus
  moves (draws `D` as above) with probability `r_c`, else stays. Queue durations are
  geometric. With `r_c = 1 − a_c` this is exactly i.i.d. "zero-inflated" steps, so the
  mode nests the current behaviour, and `r_c < 1 − a_c` adds the persistence real
  queues have. Repeated pings at the same position observe it directly.
- **Lights, stops, holds:** as now (P(red) then a phase-type wait; P(stop) then a
  phase-type dwell; a timepoint hold until the scheduled departure), but **at the
  elements' exact positions**.
- **Crossing elements within a step.** If `[x, x + D]` contains element e, the bus
  arrives there: with P(red) / P(stop) it waits at e (losing the rest of that second,
  so ≤ 1 s per actual wait). Otherwise it **carries on to x + D** with no forced pause,
  checking any further elements it crosses in the same step, in order. The step's speed
  is that of the cell where it started.
- **End of trip:** reaching L ends the trip (absorbing), as now.
- **Observations:** `y ~ (1 − ε) Normal(x, σ) + ε Uniform(0, L)`, with x the true
  position (no cell rounding).
- **Initial state:** uniform over positions (moving, or held at an early timepoint) at
  the first fix, as now. The likelihood is conditional on the first observation.

### Parameters

| Parameter | Proposed default for V1 (to confirm) |
|---|---|
| Parameter cells | the road segments between elements, as now, so comparisons are like for like |
| `μ_c` (log step length, ft) | one per parameter cell |
| `σ_c` (log-scale spread) | **one per layout** at first: with 30 s pings only the spread of the ~30 s average speed is seen, so per-cell σ is weakly identified |
| `a_c` (queue entry per step) | one per parameter cell |
| `r_c` (queue exit per step) | one per layout at first |
| lights, stops, holds, σ, ε | unchanged |

Splitting long segments into several parameter cells, and per-cell σ and r, are
options to evaluate after V1 (by held-out likelihood), not part of it.

## Computation: a fine numerical grid

Keep the exact-likelihood, gradient-MLE approach by computing the continuous model on a
**numerical grid**. The grid is not part of the model: it is an approximation that is
refined until the answer stops changing. This separates the *parameter cells* (large,
part of the model) from the *grid* (fine, numerical only), which the discrete model
conflates.

- **Grid:** spacing `h` = 5 ft to start (`GRID_FT`). The state is grid points ×
  {moving, queued}, plus the element phase chains and holds as now. Elements sit at the
  nearest grid point; two elements within one grid step share a point, applied in order.
- **Displacement kernel:** for each parameter cell, the probability of moving `k`
  grid steps, `k = 0 … K` with K = ⌈v_max / h⌉ (v_max ≈ 90 ft/s, so K = 18). The
  discrete kernel must **match the mean and variance of D**, not just bin its CDF.
  Binning each step independently adds ~h²/12 of variance per step (Sheppard), ≈ 8 ft
  of SD over 30 s at h = 5 ft, comparable to the real spread. Matching the variance
  needs Var(D) ≳ h²/4, i.e. `h ≲ 2 · SD(D)`. At 10 ft/s with a 30% CV, SD(D) = 3 ft
  and h = 5 ft is just inside that, so this bounds h. Clamp, and record when the clamp
  is hit.
- **Observation term:** integrate the normal over each grid point's interval
  (`(Φ((y − x + h/2)/σ) − Φ((y − x − h/2)/σ)) / h`), since h is not ≪ σ.
- **Step:** the same flat `index_add` scatter as `ForwardModel._step`. A precomputed
  table gives, for each (grid point, k), the destination and the elements crossed (at
  most a few). Mass splits into "waits at the first crossed element", "waits at the
  second" and so on, and "reaches x + kh", with weights from the products of
  (1 − P(red/stop)).
- **Cost (route-4 shape, ~45,000 ft):** ~9,100 grid points × 19 offsets ≈ 170k
  multiply-adds per trip-step, against 7,264 now: **~20–25×**. Autograd memory scales
  the same way: ~5 GB per trip-hour instead of ~300 MB. So V1 needs
  `torch.utils.checkpoint` over the steps between pings (or forward–backward with
  Fisher's identity), and should run on the remote machine (see
  [simulation-study.md](simulation-study.md)), with GPU support if it's slow.
  Trimming each cell's kernel to its effective support is a later optimisation.

## Implementation plan

Keep the discrete model unchanged alongside, for comparison.

1. `model/continuous/` (or similar):
   - `layout.py`: `GridLayout` with the grid, parameter cells, elements at exact
     positions, and crossing tables.
   - `chain.py`: `ContinuousParams`, a readable reference that defines the model state
     by state on the grid (dense likelihood, small layouts only), and an **exact
     continuous-space simulator** that is not on the grid: real-valued positions,
     log-normal draws, exact element crossings.
   - `forward.py`: the batched torch forward pass and forecast, with checkpointing.
     As now, **the torch forward pass must agree with the reference**; change both
     together.
2. Reuse `TripObservations` / `make_batch` (they hold steps, distances and scheduled
   holds, nothing cell-specific). `by_layout` builds a `Layout`, so it needs a variant or
   a parameter. Generalise `fit.py` to take either model (a small protocol over
   `log_likelihood` / `Parameters`).
3. Scripts: a `--model {discrete,continuous}` option on `fit_shape.py`,
   `simulation_study.py`, `fisher_information.py` and `prediction_study.py`.
4. Update AGENTS.md (layout, conventions) and bus-position-model.md.

### Tests

- The torch forward pass matches the dense reference on a tiny layout to 1e-9.
- **Grid convergence:** on simulated trips, the log-likelihood changes by a negligible
  amount (to be set) when h is halved (10 → 5 → 2.5 ft).
- The kernel's mean and variance match D's across a range of (μ, σ) and h.
- The simulator matches the propagated grid chain (position distributions after t
  steps) up to grid error.
- With `r = 1 − a`, queued steps behave as i.i.d. zero-inflated steps.
- Parameter recovery: MLE on trips from the continuous simulator beats the true
  parameters' likelihood and recovers μ_c, a, σ and the observation σ.

## Evaluation: deciding what comes after V1

Both models give a density for y in feet, so their **held-out log-likelihood per
observation is directly comparable**. Compare them on:

1. **Simulated data**, with each model fitted to trips from both simulators.
2. **Real data**, when there are a few weeks of it (the same held-out split as the
   discrete model).
3. **Forecasts** (`forecast_targets` / `score_forecasts`): error and interval coverage
   against horizon. Does the forecast median still lag, especially just before
   elements?

Diagnostics that point to the next fix:

- **Correlation of standardised forecast residuals (PIT values) between successive
  pings.** If it's clearly positive, speed needs memory (a persistent speed state).
  That's the expensive next step (~80× the current cost), so only take it on evidence.
- Interval coverage that is too narrow at long horizons: a day-level effect (the
  per-trip slowdown factor deferred in bus-position-model.md).
- The share of ping pairs with zero displacement, and queue durations, observed vs
  simulated: whether one shared r and geometric queues are enough.
- Residuals by position within long segments: whether parameter cells should be split.
- How often the variance clamp is hit: whether h must shrink.

## Open questions (to settle with the user before implementing)

- Does the queued mode belong in V1? Without it, a bus stopped in traffic away from an
  element can only be explained by the outlier term.
- The parameter-sharing defaults in the table above.
- Grid spacing and v_max, and the grid-convergence tolerance.
- Whether the 1 s time step should stay (it costs ≤ 1 s per actual wait).
