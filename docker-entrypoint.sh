#!/bin/sh
set -e

# Runs with the image's venv python directly (not `uv run`), so it works as any
# non-root user: uv would want a writable cache and venv.

# If no arguments provided, default to running scripts/collect.py
if [ $# -eq 0 ]; then
    exec python -u scripts/collect.py
fi

TARGET="$1"

# Case 1: Argument is a filename in scripts/ (e.g. "collect.py")
if [ -f "scripts/${TARGET}" ]; then
    shift
    exec python -u "scripts/${TARGET}" "$@"

# Case 2: Argument is path like "scripts/collect.py"
elif [ -f "${TARGET}" ] && [ "$(dirname "${TARGET}")" = "scripts" ]; then
    shift
    exec python -u "${TARGET}" "$@"

# Case 3: Argument is a script without extension (e.g. "collector_status")
elif [ -f "scripts/${TARGET}.py" ]; then
    shift
    exec python -u "scripts/${TARGET}.py" "$@"

# Case 4: pytest runs from the venv (no cache dir: /app isn't writable)
elif [ "$TARGET" = "pytest" ]; then
    shift
    exec python -m pytest -p no:cacheprovider "$@"

# Case 5: Any other command (python, uv, bash, sh, ...)
else
    exec "$@"
fi
