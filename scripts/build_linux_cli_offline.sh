#!/usr/bin/env bash
# Build a Linux CLI *wheelhouse* on a trusted Ubuntu 24.04 x86_64 builder.
# Real site data is never read, uploaded or bundled. System GDAL remains external.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

if [[ $# -ne 1 ]]; then
  echo "Usage: bash scripts/build_linux_cli_offline.sh /absolute/new/output-directory" >&2
  exit 2
fi
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo "Only Linux x86_64 native builders are supported (no Windows cross-build)." >&2
  exit 2
fi
if [[ "$(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:2])))')" != "3.12" ]]; then
  echo "Build with Python 3.12 for the Ubuntu 24.04 CLI wheelhouse." >&2
  exit 2
fi

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
output="$1"
if [[ "$output" != /* || -e "$output" || -L "$output" || ! -d "$(dirname -- "$output")" ]]; then
  echo "Output must be a NEW absolute local directory with an existing parent." >&2
  exit 2
fi
case "$output" in
  "$repo"/*) echo "Choose a build output OUTSIDE the source checkout." >&2; exit 2 ;;
esac
command -v sha256sum >/dev/null
command -v tar >/dev/null
command -v git >/dev/null

commit="$(git -C "$repo" rev-parse --short HEAD)"
name="GeoPublisher-CLI-Ubuntu24-x86_64-$commit"
stage="$output/$name"
mkdir -p "$stage/wheels" "$output/_build-wheel"
# Only application code, not original data, is built into the pure-Python wheel.
python3 -m build --wheel --outdir "$output/_build-wheel" "$repo"
mapfile -t own_wheels < <(find "$output/_build-wheel" -maxdepth 1 -type f -name 'geopublisher_workbench-*-py3-none-any.whl' | sort)
if (( ${#own_wheels[@]} != 1 )); then
  echo "Exactly one universal GeoPublisher wheel is required." >&2
  exit 3
fi

# This step uses public package indexes on the *builder* only.
# The installed bundle later uses --no-index exclusively.
python3 -m pip download --only-binary=:all: --dest "$stage/wheels" "${own_wheels[0]}"
cp -a -- "${own_wheels[0]}" "$stage/wheels/"
find "$stage/wheels" -maxdepth 1 -type f ! -name '*.whl' -print -quit | grep -q . && {
  echo "A source distribution or unexpected artifact reached the wheelhouse." >&2; exit 3;
}
if [[ -n "$(find "$stage/wheels" -type l -print -quit)" ]]; then
  echo "Symlinks are not accepted in the Linux offline wheelhouse." >&2; exit 3
fi

cp -- "$repo/scripts/install_linux_cli_offline.sh" "$stage/install.sh"
cp -- "$repo/docs/OFFLINE-OPERATOR.md" "$stage/OFFLINE-OPERATOR.md"
cp -- "$repo/docs/FIELD-SHEET-SPLIT-P0B.md" "$stage/FIELD-SHEET-SPLIT-P0B.md"
cp -- "$repo/docs/SHEET-BOUNDARY-P0C.md" "$stage/SHEET-BOUNDARY-P0C.md"
cat > "$stage/README.txt" <<EOF
GeoPublisher Linux x86_64 CLI wheelhouse for Ubuntu 24.04 / Python 3.12
Source commit: $commit
This is NOT a self-contained Linux executable: Python 3.12 + venv + system GDAL >=3.7/PROJ
must already be installed from approved local packages on the target host.
Offline install: bash install.sh /absolute/new/install-directory
No sudo is needed. No PyPI, conda or network access is used during installation.
Contents: application wheel + Linux third-party binary wheels + SHA256SUMS.
No site geodata, database passwords, database access or GeoServer writes.
EOF
(
  cd "$stage"
  find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
)
tar -czf "$output/$name.tar.gz" -C "$output" "$name"
sha256sum "$output/$name.tar.gz" > "$output/$name.tar.gz.sha256.txt"
echo "OFFLINE_LINUX_CLI_ARCHIVE=$output/$name.tar.gz"
echo "SYSTEM_GDAL_PREINSTALLED_REQUIRED=YES"
echo "SOURCE_COMMIT=$commit"
