# Simulation study: can the model recover its parameters?

Run on a fast machine (the M2 Mac), not the Pi: it needs no collected data, only the code.
Background and the first result (seed 0, 40 trips) are in
[bus-position-model.md](bus-position-model.md), "Parameter recovery (simulation)".

## Question

At the feed's ~30 s position cadence, which parameters does the model identify?
Seed 0 recovered the noise σ, wait and dwell lengths and long-segment speeds, but left
P(red), P(stop) and short-segment speeds near their starting values. Is that **weak
identification** (more data won't help much, so parameters need pooling), or **too little
data / an unconverged fit** (errors fall like 1/√n with more trips)?

## Setup (Mac)

```bash
git clone <github url> AAATA-GTFS && cd AAATA-GTFS
git checkout <branch>        # worktree-phase2-model, unless it has been merged
uv sync --extra model        # PyPI torch (the CPU-only index rule is for Linux aarch64 only)
uv run pyrefly check && uv run pytest
```

The fits run in **float64 on the CPU**. Apple MPS has no float64, so don't move them to
`mps`. Use `--threads` up to the number of performance cores (8 on an M2 Pro/Max, 4 on a
base M2). Run one study at a time: each one uses every thread you give it.

## Runs

Each run prints its results and saves the true and fitted parameters with `--out`. Keep
the logs and `.npz` files together:

```bash
mkdir -p study && cd study
T=8   # torch threads

# 1. Seeds: is seed 0's pattern typical? (40 trips, 2 mi; ~15 min each on the Pi)
for s in 0 1 2 3 4; do
  uv run ../scripts/simulation_study.py --seed $s --threads $T --out seed$s.npz > seed$s.log
done

# 2. More trips: do the errors shrink like 1/sqrt(n)? (same truth: seed 0)
for n in 80 160; do
  uv run ../scripts/simulation_study.py --seed 0 --trips $n --threads $T --out seed0_n$n.npz > seed0_n$n.log
done

# 3. A full-length route (8 mi, ~1,700 cells, ~40 min trips; about 9 GB memory with 40 trips)
uv run ../scripts/simulation_study.py --length-mi 8 --seed 0 --threads $T --out len8_seed0.npz > len8_seed0.log
```

Roughly: run 1 takes ~5 × 15 min on the Pi (expect several times faster on the Mac); run 2
scales about linearly with trips; run 3 is about 4× longer per trip than run 1. If memory is
tight in run 3, try `--trips 20` first. L-BFGS stops after `--max-iter` evaluations (default
150). If a log's fitted *training* log-likelihood is below the truth's, the fit hadn't
converged: rerun it with `--max-iter 400`.

## What each log reports

- Mean log-likelihood per observation for the start, the truth and the fit, on the
  training trips and on held-out simulated trips.
- Per parameter group: RMSE at the start, RMSE, bias and MAE after fitting. The groups are
  segment speed (mph, plain and length-weighted), P(red), mean red wait (s), P(stop), mean
  dwell (s), expected delay per pass at lights and at stops (P × mean wait, s), noise σ
  (ft) and the outlier rate. The `.npz` holds the raw `true_*` and `fitted_*` arrays
  (`ModelParams` fields).

## For the session that continues this

1. Tabulate the runs: per group, mean ± sd of RMSE and bias over seeds (run 1); RMSE
   against trips (40 / 80 / 160, run 2; slope about −½ on a log–log plot means
   consistent but data-limited); and route length (run 3). Add the table to
   bus-position-model.md under "Parameter recovery (simulation)".
2. If P(red) and P(stop) stay flat while the expected delay is recovered, that's weak
   identification of the split between them. Confirm it with Fisher-information standard
   errors at the fitted values: the inverse Hessian of the summed log-likelihood, from
   `torch.autograd.functional.hessian` over the flattened raw parameters, or the
   per-trip score outer product (cheaper, using the Fisher identity). This isn't
   implemented yet. Compare the standard errors with the observed errors.
3. Then **discuss the remedy with the user before implementing it**: pooling P(stop) /
   P(red) across elements (shared, or hierarchical / ridge towards a route-level mean),
   reparameterising by expected delay, or accepting it for prediction (only the delay
   matters for arrival times). Don't change the model unilaterally.
4. Keep `tasks/bus-position-model.md` and AGENTS.md current, and commit the summary (not
   the `.npz` / log files, unless the user wants them).
