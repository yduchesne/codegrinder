#!/usr/bin/env bash
# Install uv from the official Astral installer, install Python 3.14, then prepare Codegrinder.
set -Eeuo pipefail

project_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
    command -v curl >/dev/null 2>&1 || {
        echo "ERROR: curl is required to install uv." >&2
        exit 1
    }
    command -v sh >/dev/null 2>&1 || {
        echo "ERROR: sh is required to install uv." >&2
        exit 1
    }
    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "${tmp_dir}"' EXIT
    # Fetch separately so download failures cannot be masked by a shell pipeline.
    curl --fail --location --silent --show-error \
        https://astral.sh/uv/install.sh --output "${tmp_dir}/install-uv.sh"
    sh "${tmp_dir}/install-uv.sh"
    export PATH="${UV_INSTALL_DIR:-${HOME}/.local/bin}:${PATH}"
fi

command -v uv >/dev/null 2>&1 || {
    echo "ERROR: uv was installed but is not on PATH. Add ~/.local/bin to PATH and retry." >&2
    exit 1
}

cd "${project_dir}"
uv --version
uv python install 3.14
uv sync --group dev
# Register pre-commit; Gitleaks requires Go >=1.24.11 when the hook initializes.
uv run --no-sync pre-commit install
uv tool install --python 3.14 --editable "${project_dir}"
printf '\nCodegrinder is ready. Usage:\n  grind --help  (ensure ~/.local/bin is on PATH)\n  uv run grind --help\n  uv run grind run --help\n'

printf "Security hooks: Go >=1.24.11 is required to bootstrap the pinned Gitleaks pre-commit hook.\n"
