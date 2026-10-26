"""
Fisher-information standard errors for a `simulation_study.py` run: rebuilds
its training trips from the same seed, takes the observed information (minus
the Hessian of the summed log-likelihood) at the fitted parameters, and
propagates its pseudo-inverse to the reported quantities by the delta method.
Compares the standard errors with the actual errors against the truth, and
reports the correlation between each light's P(red) and red wait (and each
stop's P(stop) and dwell): strongly negative means only their product, the
expected delay, is identified. Needs the `model` extra.
"""
import argparse
import time
from pathlib import Path

import numpy as np
import torch

from aaata_gtfs.model.chain import ModelParams, ReferenceChain
from aaata_gtfs.model.fit import observed_information
from aaata_gtfs.model.forward import ForwardModel, Parameters
from aaata_gtfs.model.simulate import (
    FTPS_PER_MPH, random_params, segment_lengths_cells, simulate_trips, synthetic_layout,
)


def quantities(params: Parameters, used: np.ndarray, cell_ft: float) -> dict[str, torch.Tensor]:
    """The reported quantities as differentiable functions of the raw parameters
    (as in `simulate.recovery_errors`)."""
    c = params.constrained()
    advance = torch.arange(c["move_probs"].shape[1], dtype=c["move_probs"].dtype)

    def mean_wait(pi: torch.Tensor, q: torch.Tensor) -> torch.Tensor:
        stages = torch.arange(pi.shape[1], 0, -1, dtype=pi.dtype)
        return pi @ stages / q

    red_wait, dwell = mean_wait(c["light_pi"], c["light_q"]), mean_wait(c["dwell_pi"], c["dwell_q"])
    return {
        "speed_mph": (c["move_probs"] @ advance * cell_ft / FTPS_PER_MPH)[torch.as_tensor(used)],
        "p_red": c["p_red"],
        "red_wait_s": red_wait,
        "light_delay_s": c["p_red"] * red_wait,
        "p_stop": c["p_stop"][1:],  # the first stop is never arrived at
        "dwell_s": dwell[1:],
        "stop_delay_s": (c["p_stop"] * dwell)[1:],
        "sigma_ft": c["sigma_ft"].reshape(1),
        "outlier": c["outlier"].reshape(1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fisher-information standard errors for a simulation study run.")
    parser.add_argument("params", type=Path, help="The run's --out .npz")
    parser.add_argument("--trips", type=int, default=40, help="As in the run (default: 40)")
    parser.add_argument("--length-mi", type=float, default=2.0, help="As in the run (default: 2 mi)")
    parser.add_argument("--phases", type=int, default=8, help="As in the run (default: 8)")
    parser.add_argument("--seed", type=int, default=0, help="As in the run")
    parser.add_argument("--eps", type=float, default=1e-5, help="Finite-difference step in raw parameters")
    parser.add_argument("--threads", type=int, help="torch CPU threads")
    parser.add_argument("--out", type=Path, help="Save the estimates, standard errors and truth here (.npz)")
    args = parser.parse_args()
    if args.threads:
        torch.set_num_threads(args.threads)

    # Rebuild the run's route, truth and training trips (simulation_study.py's draws, in order).
    rng = np.random.default_rng(args.seed)
    layout = synthetic_layout(rng, length_ft=args.length_mi * 5280)
    true = random_params(layout, rng, args.phases)
    train = simulate_trips(ReferenceChain(layout, true), args.trips, rng, timepoints=[len(layout.stops) // 2])
    saved = np.load(args.params)
    fields = ModelParams.__dataclass_fields__
    if not all(np.allclose(saved[f"true_{k}"], getattr(true, k)) for k in fields):
        raise SystemExit("the saved true parameters don't match: check --seed, --length-mi and --phases")
    f = {k: saved[f"fitted_{k}"] for k in fields}
    fitted = ModelParams(f["move_probs"], f["p_red"], f["light_pi"], f["light_q"], f["p_stop"], f["dwell_pi"],
                         f["dwell_q"], float(f["sigma_ft"]), float(f["outlier"]))

    model = ForwardModel(layout)
    theta = Parameters(layout, n_phases=args.phases).set_from(fitted)
    t0 = time.time()
    info = observed_information(model, theta, train, eps=args.eps)
    print(f"observed information: {info.shape[0]} raw parameters, {len(train)} trips, in {time.time() - t0:.0f} s")
    eigval, eigvec = np.linalg.eigh(info)
    keep = eigval > 1e-9 * eigval.max()
    print(f"eigenvalues: {keep.sum()} kept, {(eigval < -1e-9 * eigval.max()).sum()} negative, "
          f"condition (kept) {eigval[keep].max() / eigval[keep].min():.3g}")
    cov_raw = (eigvec[:, keep] / eigval[keep]) @ eigvec[:, keep].T

    used = segment_lengths_cells(layout) > 0
    leaves = list(theta.parameters())
    est = quantities(theta, used, layout.cell_ft)
    with torch.no_grad():
        truth_theta = Parameters(layout, n_phases=args.phases).set_from(true)
        truth = {k: v.numpy() for k, v in quantities(truth_theta, used, layout.cell_ft).items()}
    flat = torch.cat(list(est.values()))
    jac = np.stack([torch.cat([g.reshape(-1) for g in torch.autograd.grad(v, leaves, retain_graph=True)]).numpy()
                    for v in flat])
    cov = jac @ cov_raw @ jac.T
    se_all = np.sqrt(np.clip(np.diag(cov), 0, None))
    sizes = [v.numel() for v in est.values()]
    offsets = np.cumsum([0, *sizes])
    index = {k: np.arange(offsets[i], offsets[i + 1]) for i, k in enumerate(est)}

    print("\nstandard error vs actual error    n   RMSE actual   RMS SE   median SE   |z|<2   mean |z|")
    out: dict[str, np.ndarray] = {}
    for name, idx in index.items():
        fit_v, true_v, se = flat[idx].detach().numpy(), truth[name], se_all[idx]
        z = (fit_v - true_v) / np.where(se > 0, se, np.nan)
        print(f"  {name:<28}{len(idx):4d} {np.sqrt(np.mean((fit_v - true_v) ** 2)):12.3f} "
              f"{np.sqrt(np.mean(se**2)):9.3f} {np.median(se):11.3f} {np.mean(np.abs(z) < 2):7.2f} "
              f"{np.nanmean(np.abs(z)):9.2f}")
        out.update({f"fitted_{name}": fit_v, f"true_{name}": true_v, f"se_{name}": se})

    def corr(a: str, b: str) -> np.ndarray:
        ia, ib = index[a], index[b]
        return cov[ia, ib] / np.sqrt(cov[ia, ia] * cov[ib, ib])

    for label, (a, b, d) in {"light": ("p_red", "red_wait_s", "light_delay_s"),
                             "stop": ("p_stop", "dwell_s", "stop_delay_s")}.items():
        r = corr(a, b)
        out[f"corr_{a}_{b}"] = r
        print(f"\n{label:<6}  {a:>7} (true, fit ± SE)        {b:>10} (true, fit ± SE)       {d} (true, fit ± SE)   corr")
        for i in range(len(index[a])):
            row = []
            for name in (a, b, d):
                j = index[name][i]
                row.append(f"{truth[name][i]:6.2f} {float(flat[j].detach()):6.2f} ± {se_all[j]:5.2f}")
            print(f"  {i:<4}  " + "    ".join(row) + f"   {r[i]:+.2f}")
    if args.out is not None:
        np.savez(args.out, allow_pickle=False, eigenvalues=eigval, **out)
        print(f"saved to {args.out}")


if __name__ == "__main__":
    main()
