# Simulation study: can the model recover its parameters, and does it predict?

Run on a big remote machine: it needs no collected data, only the code. It was started
on the M2 Pro (16 GB) on 2026-10-25, which ran out of memory; the results so far are
below. Background and the first result are in
[bus-position-model.md](bus-position-model.md), "Parameter recovery (simulation)".

## Questions

1. **Identification.** At the feed's ~30 s position cadence, which parameters does
   the model identify? P(red), P(stop) and short-segment speeds stay near their
   starting values with 40 trips. Is that weak identification (more data won't help
   much, so parameters need pooling), or too little data / an unconverged fit (errors
   fall like 1/√n with more trips)?
2. **Prediction.** Even where parameters aren't identified, are the position forecasts
   (h seconds ahead, given the pings so far) as good as the true parameters'? That's
   what matters for arrival prediction.

## Setup (remote machine)

```bash
git clone https://github.com/Veldrovive/AAATA-GTFS && cd AAATA-GTFS
git checkout worktree-simulation-study   # unless it has been merged
uv sync --extra model                     # torch + matplotlib
uv run pyrefly check && uv run pytest     # 74 pass offline
```

- The fits run in **float64 on the CPU**; GPUs aren't used (Apple MPS has no float64;
  CUDA would need a `--device` option the scripts don't have yet). On Linux x86_64,
  PyPI's torch wheel pulls in CUDA libraries (a few GB of download) but runs fine on CPU.
- torch threads beyond ~8 per process gain little (the forward pass is a long sequence of
  small steps), so on a many-core machine run several jobs in parallel instead.
- Use `tmux` / `nohup` so the runs survive a dropped SSH session.

## Run

One command runs everything: the fits, then Fisher standard errors and forecast
evaluations. `JOBS × THREADS` should be at most the physical cores:

```bash
JOBS=4 THREADS=8 nohup scripts/run_simulation_study.sh study > study.out 2>&1 &
tail -f study.out    # start / done / FAIL per step, with times
```

`scripts/run_simulation_study.sh` lists every step. All fits use `--max-iter 400` (the
M2 runs used the default 150 and every one stopped at the cap). Steps whose output
exists are skipped, so rerun it after a failure or to add steps. Each step writes
`study/<name>.log` and its `.npz` / `.png` / `.csv`.

| Step | What | Cost (M2 Pro, 6 threads, 150 evaluations) | Memory |
|---|---|---|---|
| `seed0`–`seed4` | 2 mi route, 40 trips, five truths | ~3 min each | ~1.8 GB |
| `seed0_n80`, `_n160`, `_n320` | seed 0 with more trips | ~linear in trips: 9, 18, ~36 min | ~linear |
| `len8_seed0_n20`, `len8_seed0` | 8 mi route (~1,700 cells, ~40 min trips) | ~16× per trip; didn't finish in 30 min | ~9 GB at 40 trips |
| `fisher_*` | observed information at each 2 mi fit | 7 min at 40 trips (4 threads), 19 min at 80 | ~1 GB at 160 trips |
| `prediction_*` | forecast evaluation on 100 fresh trips (50 on 8 mi) | ~1 min per parameter set per 20 trips (2 mi) | `--chunk` trips at once |

With 400 evaluations, expect up to ~2.6× the fit times above.

### The scripts

- `scripts/simulation_study.py`: simulate trips from random true parameters on a
  synthetic route, fit by L-BFGS from uninformed values, report errors by parameter
  group (RMSE start / fitted, bias, MAE) and the log-likelihood per observation (start,
  truth, fit; training and held out). `--out` saves `true_*` and `fitted_*` arrays.
- `scripts/fisher_information.py <run>.npz --seed S [--trips N --length-mi L]`: rebuilds
  the run's training trips from the seed (`simulate.study_setup`) and takes the observed
  information (`fit.observed_information`: minus the Hessian of the summed
  log-likelihood over the raw parameters, by central differences of the exact gradient;
  2 × n_params gradient evaluations). It propagates its pseudo-inverse to the reported
  quantities by the delta method and prints, per group, the RMS standard error against
  the actual RMSE, the share of |z| < 2, and, per light and stop, P, mean wait and delay
  with standard errors and the P–wait correlation. A correlation near −1 means only
  the delay is identified.
- `scripts/prediction_study.py <fits>.npz ... --seed S [--length-mi L]`: simulates fresh
  trips from the same truth and forecasts from every ping (`ForwardModel.forecast`:
  filter to the ping, then propagate without observations) with the truth, each fit, the
  uninformed start and a constant-speed baseline (last ping + the median route speed).
  It scores each horizon against the noise-free simulated position: mean |forecast median
  − truth|, CRPS, and coverage of the 80% interval. Positions are scored only while the
  bus is still on its trip, and horizons where fewer than `--min-share` (20%) of
  forecasts are still scorable are dropped (the longest ones are slow trips only). It
  prints a table, writes `--csv` and plots `--plot` (three panels against the horizon in
  seconds).

## Results so far (M2 Pro, 150 evaluations, before it was stopped)

The 2 mi route has 424 cells, 8 stops, 5 lights and 14 segments. Each fit ran 152–156
evaluations, so it hit the cap. Its training log-likelihood was above the truth's every
time (e.g. seed 0: −6.052 vs −6.101), so the fits are near an optimum but not certainly
converged.

**Seeds 0–4, 40 trips** (mean ± sd over seeds):

| | RMSE start | RMSE fitted | bias | RMS Fisher SE | share \|z\| < 2 |
|---|---|---|---|---|---|
| segment speed, mph | 5.3 ± 0.2 | 5.1 ± 2.7 | −0.1 ± 1.6 | 4.5 ± 2.3 | 0.83 |
| speed, length-weighted | 5.2 ± 0.4 | 2.7 ± 1.2 | −0.0 ± 0.6 | | |
| P(red) | 0.12 ± 0.01 | 0.13 ± 0.09 | +0.03 ± 0.06 | 0.17 ± 0.15 | 0.92 |
| mean red wait, s | 30.9 ± 4.6 | 6.6 ± 2.9 | −1.9 ± 3.6 | 10.1 ± 7.5 | 0.92 |
| P(stop) | 0.22 ± 0.03 | 0.23 ± 0.10 | −0.06 ± 0.05 | 0.17 ± 0.08 | 0.69 |
| mean dwell, s | 18.2 ± 2.7 | 4.8 ± 1.6 | +1.4 ± 1.8 | 7.5 ± 5.4 | 0.89 |
| light delay, s | 16.0 ± 2.3 | 4.7 ± 1.7 | −0.6 ± 1.7 | 4.4 ± 0.5 | 0.84 |
| stop delay, s | 12.9 ± 3.3 | 4.0 ± 0.9 | −0.6 ± 2.0 | 3.2 ± 0.5 | 0.77 |
| noise σ, ft | 30.0 | 0.6 ± 0.6 | −0.6 ± 0.6 | 0.70 ± 0.04 | 0.80 |

**Seed 0, more trips** (RMSE fitted at 40 / 80 / 160 trips, and the log–log slope):

| | 40 | 80 | 160 | slope |
|---|---|---|---|---|
| segment speed, mph | 5.21 | 2.77 | 1.07 | −1.1 |
| speed, length-weighted | 3.15 | 1.90 | 0.81 | −1.0 |
| P(red) | 0.117 | 0.073 | 0.051 | −0.6 |
| mean red wait, s | 9.3 | 5.1 | 5.0 | −0.5 |
| P(stop) | 0.214 | 0.069 | 0.091 | −0.6 |
| mean dwell, s | 6.0 | 3.9 | 3.2 | −0.5 |
| light delay, s | 4.7 | 3.7 | 3.1 | −0.3 |
| stop delay, s | 4.2 | 2.1 | 1.4 | −0.8 |
| noise σ, ft | 1.58 | 0.73 | 0.32 | −1.1 |

**Fisher information (seed 0, 40 trips).** 188 raw parameters. Eigenvalues span 655
down to flat directions, about 60 of them at ≈ 0: the softmax constants, plus the
phase-type wait *shapes* beyond their means. The 15 slightly negative ones (≥ −0.02) are
in those flat directions too. The standard errors match the observed errors (mean |z| ≈
1 in every group except σ). P(red) SEs are 0.08–0.18, about the spread of the truth
itself (uniform 0.2–0.7, sd 0.14), so 40 trips carry almost no information about it. The
P–wait correlations are mostly −0.1 to −0.6, with a few stops at −0.8 to −0.9.

**Forecasts (seed 0 fit, 20 trips, trial run).** The 40-trip fit forecasts as well as
the true parameters at every horizon: |median − truth| 438 vs 435 ft at 60 s, 644 vs 619
ft at 120 s, 850 vs 846 ft at 240 s. The 80% intervals cover 80–87% of true positions.
The constant-speed baseline is 10–30% worse (579, 713, 957 ft); the uninformed start
2–3× worse.

**Reading so far.** The errors fall with the number of trips and the Fisher standard
errors account for them. So P(red) and P(stop) look **data-limited (≈ 1/n^0.6), not
unidentified**: 40 trips is simply very little information about them at a 30 s
cadence. The split between P and the mean wait is only partly confounded. Forecasts
don't suffer from it. To be confirmed by the full run (more trips, converged fits, five
seeds of forecasts, the 8 mi route).

## For the session that continues this

1. Tabulate the full run like the tables above: per group over seeds; RMSE against
   trips (40–320, slope); route length (8 mi with 20 and 40 trips); 150 vs 400
   evaluations (does seed 0 change?). Check each log's fitted *training*
   log-likelihood is above the truth's. Add the summary to bus-position-model.md under
   "Parameter recovery (simulation)", and bring the forecast plots to the user.
2. Fisher: compare standard errors with the actual errors (the share |z| < 2 should be
   near 0.95), and do the standard errors shrink like 1/√n across 40–320 trips? Look at
   the P–wait correlations per element: which stops or lights are near −1, and what's
   special about them (short dwells? rarely stopped?).
3. Forecasts: do the fits match the truth at every horizon on every seed, and on the
   8 mi route? Is the 80% interval calibrated? How much better than constant speed?
4. Then **discuss the remedy with the user before implementing it**: pooling P(stop) /
   P(red) across elements (shared, or hierarchical / ridge towards a route-level mean),
   reparameterising by expected delay, or accepting it for prediction. Don't change the
   model unilaterally.
5. Keep `tasks/bus-position-model.md` and AGENTS.md current, and commit the summary (not
   the `.npz` / log files, unless the user wants them).
