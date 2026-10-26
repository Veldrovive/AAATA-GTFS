#!/usr/bin/env bash
# The full simulation study (tasks/simulation-study.md): fits, then Fisher
# standard errors and forecast evaluations of the fits. Run from the repo root:
#
#   JOBS=4 THREADS=8 scripts/run_simulation_study.sh [out dir, default study]
#
# JOBS runs in parallel with THREADS torch threads each (JOBS × THREADS ≤ physical
# cores). Each step's output goes to <out>/<name>.log; finished steps (their
# output file exists) are skipped, so the script can be rerun after a failure.
set -euo pipefail
O=${1:-study}
JOBS=${JOBS:-2}
THREADS=${THREADS:-4}
MAX_ITER=${MAX_ITER:-400}
mkdir -p "$O"

# Each line: name, then the arguments of one script run. The script writes $O/<name>.npz / .png.
run_all() {
  grep -v '^\s*$' | xargs -P "$JOBS" -L 1 bash -c '
    name=$1; script=$2; shift 2
    target=$O/$name.npz; [ "$script" = prediction_study ] && target=$O/$name.csv
    if [ -f "$target" ]; then echo "skip $name"; exit 0; fi
    echo "start $name $(date +%T)"
    if uv run "scripts/$script.py" --threads "$THREADS" "$@" > "$O/$name.log" 2>&1; then
      echo "done  $name $(date +%T)"
    else
      echo "FAIL  $name (see $O/$name.log)"
    fi' _
}
export O THREADS

fit="simulation_study --max-iter $MAX_ITER"
# 1. Fits: five seeds; 40–320 trips on seed 0; the 8 mi route with 20 and 40 trips.
run_all <<EOF
seed0 $fit --seed 0 --out $O/seed0.npz
seed1 $fit --seed 1 --out $O/seed1.npz
seed2 $fit --seed 2 --out $O/seed2.npz
seed3 $fit --seed 3 --out $O/seed3.npz
seed4 $fit --seed 4 --out $O/seed4.npz
seed0_n80 $fit --seed 0 --trips 80 --out $O/seed0_n80.npz
seed0_n160 $fit --seed 0 --trips 160 --out $O/seed0_n160.npz
seed0_n320 $fit --seed 0 --trips 320 --out $O/seed0_n320.npz
len8_seed0_n20 $fit --length-mi 8 --seed 0 --trips 20 --out $O/len8_seed0_n20.npz
len8_seed0 $fit --length-mi 8 --seed 0 --out $O/len8_seed0.npz
EOF

# 2. Fisher-information standard errors (2 mi fits; the 8 mi route has ~4× the
#    parameters and ~16× the cost per gradient, so it's left out), and forecasts.
run_all <<EOF
fisher_seed0 fisher_information $O/seed0.npz --seed 0 --out $O/fisher_seed0.npz
fisher_seed1 fisher_information $O/seed1.npz --seed 1 --out $O/fisher_seed1.npz
fisher_seed2 fisher_information $O/seed2.npz --seed 2 --out $O/fisher_seed2.npz
fisher_seed3 fisher_information $O/seed3.npz --seed 3 --out $O/fisher_seed3.npz
fisher_seed4 fisher_information $O/seed4.npz --seed 4 --out $O/fisher_seed4.npz
fisher_seed0_n80 fisher_information $O/seed0_n80.npz --seed 0 --trips 80 --out $O/fisher_seed0_n80.npz
fisher_seed0_n160 fisher_information $O/seed0_n160.npz --seed 0 --trips 160 --out $O/fisher_seed0_n160.npz
fisher_seed0_n320 fisher_information $O/seed0_n320.npz --seed 0 --trips 320 --out $O/fisher_seed0_n320.npz
prediction_seed0 prediction_study $O/seed0.npz $O/seed0_n80.npz $O/seed0_n160.npz $O/seed0_n320.npz --seed 0 --plot $O/prediction_seed0.png --csv $O/prediction_seed0.csv
prediction_seed1 prediction_study $O/seed1.npz --seed 1 --plot $O/prediction_seed1.png --csv $O/prediction_seed1.csv
prediction_seed2 prediction_study $O/seed2.npz --seed 2 --plot $O/prediction_seed2.png --csv $O/prediction_seed2.csv
prediction_seed3 prediction_study $O/seed3.npz --seed 3 --plot $O/prediction_seed3.png --csv $O/prediction_seed3.csv
prediction_seed4 prediction_study $O/seed4.npz --seed 4 --plot $O/prediction_seed4.png --csv $O/prediction_seed4.csv
prediction_len8_seed0 prediction_study $O/len8_seed0_n20.npz $O/len8_seed0.npz --seed 0 --length-mi 8 --eval-trips 50 --max-horizon 1800 --horizon-step 30 --chunk 5 --plot $O/prediction_len8_seed0.png --csv $O/prediction_len8_seed0.csv
EOF
echo "all done: $O"
