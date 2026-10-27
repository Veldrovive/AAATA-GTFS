"""
Parameter recovery: simulates trips from known parameters on a synthetic route
at the feed's cadence, fits the model by MLE from uninformed starting values,
and reports the errors against the truth in physical units, alongside the
starting errors and the log-likelihood on held-out simulated trips. Needs the
`model` extra.
"""
import argparse
import math
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import torch

from aaata_gtfs.model.chain import ReferenceChain
from aaata_gtfs.model.fit import fit_lbfgs, mean_log_likelihood
from aaata_gtfs.model.forward import ForwardModel, Parameters
from aaata_gtfs.model.simulate import random_params, recovery_errors, simulate_trips, synthetic_layout


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate trips, fit the model, and compare with the truth.")
    parser.add_argument("--trips", type=int, default=40, help="Training trips (default: 40)")
    parser.add_argument("--test-trips", type=int, default=20, help="Held-out trips (default: 20)")
    parser.add_argument("--length-mi", type=float, default=2.0, help="Route length (default: 2 mi)")
    parser.add_argument("--phases", type=int, default=8, help="Phases per wait / dwell (default: 8)")
    parser.add_argument("--max-iter", type=int, default=150, help="L-BFGS iterations (default: 150; each runs one or more evaluations)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--threads", type=int, help="torch CPU threads")
    parser.add_argument("--out", type=Path, help="Save the true and fitted parameters here (.npz)")
    args = parser.parse_args()
    if args.threads:
        torch.set_num_threads(args.threads)

    rng = np.random.default_rng(args.seed)
    layout = synthetic_layout(rng, length_ft=args.length_mi * 5280)
    true = random_params(layout, rng, args.phases)
    chain = ReferenceChain(layout, true)
    timepoints = [len(layout.stops) // 2]
    t0 = time.time()
    train = simulate_trips(chain, args.trips, rng, timepoints=timepoints)
    test = simulate_trips(chain, args.test_trips, rng, timepoints=timepoints)
    n_obs = sum(len(t.steps) for t in train)
    print(f"route: {args.length_mi:g} mi, {layout.n_cells} cells, {len(layout.stops)} stops "
          f"(timepoints: first and #{timepoints[0]}), {len(layout.lights)} lights, {layout.n_segments} segments")
    print(f"simulated {len(train)} training trips ({n_obs} observations, median "
          f"{np.median([t.steps[-1] for t in train]) / 60:.1f} min) and {len(test)} held-out, "
          f"in {time.time() - t0:.0f} s")

    model = ForwardModel(layout)
    truth = Parameters(layout, n_phases=args.phases).set_from(true)
    # Uninformed start: uniform advances, all probabilities 1/2, 50 ft noise.
    theta = Parameters(layout, n_phases=args.phases)
    with torch.no_grad():
        theta.log_sigma.fill_(math.log(48.0))
    start = theta.to_params()
    lls = {"start": (mean_log_likelihood(model, theta, train), mean_log_likelihood(model, theta, test)),
           "truth": (mean_log_likelihood(model, truth, train), mean_log_likelihood(model, truth, test))}
    t0 = time.time()
    history = fit_lbfgs(model, theta, train, max_iter=args.max_iter)
    print(f"fit: {len(history)} evaluations in {time.time() - t0:.0f} s")
    lls["fitted"] = (mean_log_likelihood(model, theta, train), mean_log_likelihood(model, theta, test))
    print("\nmean log-likelihood per observation   train      held-out")
    for name, (a, b) in lls.items():
        print(f"  {name:<36}{a:9.4f} {b:9.4f}")

    fitted = theta.to_params()
    before, after = recovery_errors(start, true, layout), recovery_errors(fitted, true, layout)
    print("\nerror vs truth                     n    RMSE start   RMSE fitted   bias fitted   MAE fitted")
    for name in after:
        b, a = before[name], after[name]
        print(f"  {name:<28}{a.n:4d} {b.rmse:12.3f} {a.rmse:13.3f} {a.bias:+13.3f} {a.mae:12.3f}")
    if args.out is not None:
        arrays: dict[str, np.ndarray] = {}
        for prefix, params in (("true", true), ("fitted", fitted)):
            arrays.update({f"{prefix}_{k}": np.asarray(v) for k, v in asdict(params).items()})
        np.savez(args.out, allow_pickle=False, **arrays)
        print(f"saved parameters to {args.out}")


if __name__ == "__main__":
    main()
