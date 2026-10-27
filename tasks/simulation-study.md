# Simulation study: can the model recover its parameters, and does it predict?

Run on a big machine: it needs no collected data, only the code. Started on the M2 Pro
(16 GB) on 2026-10-25, which ran out of memory; the full study ran on a 112-core Linux
machine (2 × 56-core Xeon, 2 TB) on 2026-10-25/26 in about 1 h 45 min. Background is in
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

## Setup

```bash
git clone https://github.com/Veldrovive/AAATA-GTFS && cd AAATA-GTFS
uv sync --extra model                     # torch + matplotlib
uv run pyrefly check && uv run pytest     # 74 pass offline
```

- The fits run in **float64 on the CPU**; GPUs aren't used (Apple MPS has no float64;
  CUDA would need a `--device` option the scripts don't have yet). On Linux x86_64,
  PyPI's torch wheel pulls in CUDA libraries (a few GB of download) but runs fine on CPU.
- torch threads beyond ~8 per process gain little (the forward pass is a long sequence of
  small steps), so on a many-core machine run several jobs in parallel instead.
- Use `tmux` / `nohup` so the runs survive a dropped SSH session. The logs fill only when
  a step ends (Python buffers its output when redirected).

## Run

One command runs everything: the fits, then Fisher standard errors and forecast
evaluations. `JOBS × THREADS` should be at most the physical cores:

```bash
JOBS=10 THREADS=6 nohup scripts/run_simulation_study.sh study > study.out 2>&1 &
tail -f study.out    # start / done / FAIL per step, with times
```

`scripts/run_simulation_study.sh` lists every step. All fits use `--max-iter 400`
(L-BFGS *iterations*; each runs one or more evaluations, so a fit that stops at the cap
reports a few more than 400 evaluations). Steps whose output exists are skipped, so rerun
it after a failure or to add steps; but a step started by hand that hasn't finished yet
has no output, and the script would start it a second time. Each step writes
`study/<name>.log` and its `.npz` / `.png` / `.csv`.

Costs measured on the Xeon, 6 threads per job, 10 jobs at once:

| Step | What | Time | Memory |
|---|---|---|---|
| `seed0`–`seed4` | 2 mi route, 40 trips, five truths | 8–14 min | ~2.5 GB |
| `seed0_n80`, `_n160`, `_n320` | seed 0 with more trips | 14, 31, 47 min | 5, 10, 20 GB |
| `len8_seed0_n20`, `len8_seed0` | 8 mi route (1,694 cells, ~36 min trips) | 53, 91 min | 11, 25 GB |
| `fisher_*` | observed information at each 2 mi fit | 10, 17, 29, 48 min at 40–320 trips | ~3.5 GB at 80 trips |
| `prediction_*` | forecast evaluation on 100 fresh trips (50 on 8 mi) | ~30 s per seed (2 mi); 7 min (8 mi) | `--chunk` trips at once |

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
  the delay is identified. The pseudo-inverse drops flat directions, so a parameter
  that lies along one (typically a P at 0 or 1) gets an SE near 0 and an enormous |z|:
  read the share |z| < 2 and the median SE, not the mean |z|.
- `scripts/prediction_study.py <fits>.npz ... --seed S [--length-mi L]`: simulates fresh
  trips from the same truth and forecasts from every ping (`ForwardModel.forecast`:
  filter to the ping, then propagate without observations) with the truth, each fit, the
  uninformed start and a constant-speed baseline (last ping + the median route speed).
  It scores each horizon against the noise-free simulated position: mean |forecast median
  − truth|, CRPS, and coverage of the 80% interval. By default positions are scored only
  while the bus is still on its trip, and horizons where fewer than `--min-share` (20%)
  of forecasts are still scorable are dropped. **That selects on the outcome** (a bus
  that has arrived isn't scored, but near the end a good forecast puts mass on its having
  arrived), which favours forecasts that lag: with it, constant speed looks better than
  the true parameters at the longest 2 mi horizons. `--score-arrived` scores a bus that
  has reached the end at the end, so every forecast is scored at every horizon; use it
  to compare against the baseline. It prints a table, writes `--csv` and plots `--plot`
  (three panels against the horizon in seconds).

## Results (Xeon, 400 iterations)

These ran with the old speed generator, whose segments were all ~16–55 mph (see
bus-position-model.md, "Parameter recovery"); a rerun covers 2–40 mph, slow traffic
included.

The 2 mi route has 424 cells, 8 stops, 5 lights and 14 segments. The training
log-likelihood of every fit is above the truth's (40 trips: by 0.04–0.06 per
observation), and its held-out log-likelihood below (40 trips: by 0.05–0.07; 320 trips:
0.011), so the fits reach the optimum and overfit a little at 40 trips. Seeds 1–3 and
`seed0_n80`, `_n320` stopped on tolerance (261–357 evaluations); seeds 0 and 4, `_n160`
and both 8 mi fits at the 400-iteration cap. 400 rather than 150 iterations changes
little: seed 0's errors at 40 and 160 trips are within noise of the M2's.

**Seeds 0–4, 40 trips** (mean ± sd over seeds; Fisher columns pooled over seeds):

| | RMSE start | RMSE fitted | bias | RMS Fisher SE | share \|z\| < 2 |
|---|---|---|---|---|---|
| segment speed, mph | 5.3 ± 0.2 | 4.9 ± 2.7 | −0.0 ± 1.7 | 15 ± 27 (one seed: 63) | 0.83 |
| speed, length-weighted | 5.2 ± 0.4 | 2.6 ± 1.2 | 0.0 ± 0.5 | | |
| P(red) | 0.12 ± 0.01 | 0.14 ± 0.09 | +0.05 ± 0.08 | 0.17 ± 0.14 | 0.88 |
| mean red wait, s | 30.9 ± 4.6 | 6.6 ± 3.0 | −2.5 ± 4.1 | 10.2 ± 6.8 | 0.92 |
| P(stop) | 0.22 ± 0.03 | 0.22 ± 0.10 | −0.06 ± 0.05 | 0.15 ± 0.05 | 0.69 |
| mean dwell, s | 18.2 ± 2.7 | 4.8 ± 1.6 | +1.4 ± 1.8 | 7.9 ± 6.5 | 0.89 |
| light delay, s | 16.0 ± 2.4 | 4.7 ± 1.7 | −0.5 ± 1.8 | 4.4 ± 0.5 | 0.84 |
| stop delay, s | 12.9 ± 3.3 | 4.0 ± 0.9 | −0.6 ± 2.0 | 3.2 ± 0.5 | 0.77 |
| noise σ, ft | 30.0 | 0.6 ± 0.6 | −0.6 ± 0.6 | 0.70 ± 0.04 | 0.80 |

**Seed 0, more trips** (RMSE fitted, its log–log slope, and the RMS Fisher SE):

| | 40 | 80 | 160 | 320 | slope | SE 40 / 80 / 160 / 320 | SE slope |
|---|---|---|---|---|---|---|---|
| segment speed, mph | 4.81 | 2.70 | 1.02 | 0.34 | −1.3 | 2.7 / 2.6 / 1.7 / 1.2 | −0.4 |
| speed, length-weighted | 2.97 | 1.88 | 0.79 | 0.31 | −1.1 | | |
| P(red) | 0.107 | 0.075 | 0.055 | 0.050 | −0.4 | 0.12 / 0.13 / 0.06 / 0.04 | −0.6 |
| mean red wait, s | 9.2 | 5.4 | 5.1 | 3.7 | −0.4 | 7.9 / 9.3 / 4.3 / 3.6 | −0.5 |
| P(stop) | 0.203 | 0.062 | 0.090 | 0.068 | −0.4 | 0.18 / 0.08 / 0.09 / 0.05 | −0.5 |
| mean dwell, s | 5.9 | 3.7 | 3.2 | 2.5 | −0.4 | 4.9 / 3.3 / 3.1 / 1.7 | −0.5 |
| light delay, s | 4.6 | 3.8 | 3.1 | 1.5 | −0.5 | 4.3 / 3.3 / 2.3 / 1.7 | −0.5 |
| stop delay, s | 4.1 | 2.1 | 1.4 | 0.6 | −0.9 | 3.4 / 2.3 / 1.6 / 1.1 | −0.5 |
| noise σ, ft | 1.56 | 0.73 | 0.32 | 0.085 | −1.4 | 0.65 / 0.48 / 0.36 / 0.25 | −0.5 |

**8 mi route** (1,694 cells, 31 stops, 23 lights, 55 segments; seed 0), RMSE fitted at
20 / 40 trips: length-weighted speed 4.95 / 3.59 mph (segment speed 9.2 / 8.3: the short
segments), P(red) 0.126 / 0.135, red wait 9.6 / 7.9 s, P(stop) 0.245 / 0.229, dwell 10.4 /
9.8 s, light delay 5.5 / 3.7 s, stop delay 4.8 / 3.4 s, σ 1.4 / 0.9 ft. Per element that's
about the 2 mi route's at 40 trips (each element still sees 40 passes), with dwells
worse. 20 trips overfit clearly (held out −6.43 vs truth −6.26; 40 trips −6.39 vs −6.32).

**Fisher information.** 188 raw parameters on the 2 mi route; the pseudo-inverse keeps
55–112 directions depending on the seed (the softmax constants and phase-type wait
shapes beyond their means are flat), with 0–20 slightly negative eigenvalues. With the
flat directions dropped, the standard errors match the actual errors from 80 trips on
(at 160 trips: RMS SE vs RMSE 0.088 vs 0.090 for P(stop), 0.059 vs 0.055 for P(red),
1.6 vs 1.4 s for stop delay), and every group's RMS SE falls like n^−0.4…−0.6, i.e. the
information about every reported quantity, P(red) and P(stop) included, grows linearly
with the trips. Two exceptions: the **dwell SEs are too small** (share |z| < 2 is 0.57
at 80, 160 and 320 trips; mean |z| 2.3 at 320), presumably because the dropped
directions (the dwell shape) carry some of the mean's uncertainty, and at 40 trips the
P(stop) SEs are too small (share 0.69) for elements near P = 0 or 1. Speed, σ and stop
delay errors fall faster than their SEs (slope −0.9 to −1.4 against −0.5), so at 40
trips their errors are inflated beyond the asymptotic ones (the fits are still in the
non-linear regime).

**P–wait correlations** (per light and stop): median −0.24 at 40 trips, ranging −0.95 to
+1. The most confounded elements (−0.8 to −0.95) have ordinary truths (P 0.4–0.7, waits
10–32 s), and the correlation doesn't depend on the true P, the wait, or the gap to the
nearest other element (r = −0.05 against log gap). Last stops are a little more
confounded (median −0.41 over five). Which elements are confounded changes with the data
(seed 0's stop 5: −0.83 at 40 trips, −0.08 at 160), and at 160 trips the most confounded
are the stops with P near 1 (true 0.80 and 0.90: −0.82, −0.78) and the last stop (−0.88).

**Forecasts.** With `--score-arrived` (mean |forecast median − truth|, ft, mean over the
five 2 mi seeds; the fit is each seed's 40-trip fit):

| h | truth | fit | constant speed | uninformed start | 80% coverage (truth / fit) |
|---|---|---|---|---|---|
| 60 s | 303 | 310 | 497 | 727 | 0.90 / 0.88 |
| 240 s | 461 | 468 | 564 | 1,102 | 0.91 / 0.90 |

Every fit is within 0–6% of the truth at every horizon on every seed, and seed 0's 40,
80, 160 and 320-trip fits are indistinguishable from each other and from the truth
(e.g. 530, 531, 531, 532 vs 534 ft at 240 s). Constant speed is 18–30% worse at 240 s and
worse still in CRPS (seed 0 at 240 s: about 650 vs 375 ft). On the 8 mi route (50 trips),
the 40-trip fit's error is 333 / 734 / 916 / 617 ft at 60 / 300 / 900 / 1,500 s against
the truth's 323 / 712 / 901 / 614 and constant speed's 634 / 1,023 / 1,098 / 700; the 20-trip
fit is 2–5% behind it only up to ~5 min. The 80% intervals cover 0.86–0.93 (conservative,
probably partly from quantiles taken at 25 ft cell centres); scored the default way, on
the 8 mi route coverage falls from 0.85 to 0.76 by 25 min ahead, for the truth too.
Plots: `prediction_arrived_seed*.png`, `prediction_arrived_len8_seed0.png`.

**Reading.** P(red) and P(stop) are **identified, but each trip carries very little
information about them**: their errors and standard errors fall like 1/√n, and at 320
trips they're still ±0.04–0.07 per element. The M2's
impression of a plateau between 80 and 320 trips was one seed's noise (P(stop): 0.062,
0.090, 0.068, against SEs of 0.08, 0.09, 0.05). What the pings pin down is the expected
delay per pass (stop delay error 0.6 s at 320 trips); the split into P and the wait is
partly confounded, by an amount that varies by element and sample. **None of this
matters for forecasts**: from 40 trips on, fits forecast positions as well as the true
parameters, up to 35 min ahead on an 8 mi route, and clearly better than constant speed.

The study outputs (logs, `.npz`, `.csv`, plots) are on the Xeon in
`/scratch4/home/adempst/projects/AAATA-GTFS-study/` (not in git).

## Next

1. **Discuss the remedy with the user before implementing anything** (see
   bus-position-model.md): pooling P(stop) / P(red) across elements (shared, or
   hierarchical / ridge towards a route-level mean), reparameterising by expected delay,
   or accepting it, since forecasts don't suffer. The P estimates matter only if they're
   reported for their own sake (e.g. "how often do buses stop at stop X").
2. Whether `--score-arrived` should become `prediction_study.py`'s default.
3. The dwell standard errors are too small: if SEs are going to be reported, take them
   from the profile likelihood or a parametric bootstrap instead of the pseudo-inverse.
4. Only seed 0 was run with more than 40 trips; more seeds at 160–320 trips would firm up
   the slopes, but the Fisher SEs already agree with them.
