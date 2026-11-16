#!/usr/bin/env bash
# Install from an audited wheelhouse only. No sudo, pip index, network or GIS writes.
set -euo pipefail

if [[ $# -ne 1 || "$1" != /* ]]; then
  echo "Usage: bash install.sh /absolute/new/install-directory" >&2
  exit 2
fi
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo "This CLI wheelhouse is for Linux x86_64 only." >&2
  exit 2
fi
bundle="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
destination="$1"
if [[ -e "$destination" || -L "$destination" || ! -d "$(dirname -- "$destination")" ]]; then
  echo "Refusing to overwrite an existing install directory." >&2
  exit 2
fi
if [[ "$destination" == "$bundle"/* ]]; then
  echo "Install directory cannot be inside this wheelhouse." >&2
  exit 2
fi
for command in python3.12 sha256sum ogrinfo ogr2ogr gdalinfo gdalsrsinfo; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Missing local prerequisite: $command. Provision from approved offline packages." >&2
    exit 3
  fi
done
if [[ "$(python3.12 -c 'import sys; print(".".join(map(str,sys.version_info[:2])))')" != "3.12" ]]; then
  echo "Python 3.12 is required for these Linux wheel tags." >&2
  exit 3
fi

# Python/GDAL itself is NOT bundled; fail before creating a target folder if
# the system GDAL is absent, incompatible, or PROJ cannot resolve EPSG locally.
version="$(ogrinfo --version | grep -oE 'GDAL [0-9]+\.[0-9]+\.[0-9]+' | head -n 1 | cut -d' ' -f2)"
if [[ -z "$version" ]]; then
  echo "System GDAL version is not recognizable." >&2
  exit 3
fi
if ! python3.12 -c 'import sys; a=list(map(int,sys.argv[1].split(".")));sys.exit(0 if a >= [3,7,0] else 1)' "$version"; then
  echo "System GDAL >=3.7 is required; detected $version." >&2
  exit 3
fi
export PROJ_NETWORK=OFF
if ! gdalsrsinfo EPSG:4326 >/dev/null 2>&1; then
  echo "GDAL/PROJ local coordinate reference database is not working." >&2
  exit 3
fi
if [[ -n "$(find "$bundle" -type l -print -quit)" ]]; then
  echo "Bundle includes symlinks; refuse installation." >&2
  exit 3
fi
(
  cd "$bundle"
  listed="$(wc -l < SHA256SUMS)"
  present="$(find . -type f ! -name SHA256SUMS | wc -l)"
  if [[ "$listed" != "$present" ]]; then
    echo "Unexpected, missing or unlisted files in offline bundle." >&2
    exit 3
  fi
  sha256sum --check --status SHA256SUMS
)
echo "OFFLINE_HASHES=OK"

mapfile -t wheels < <(find "$bundle/wheels" -maxdepth 1 -type f -name 'geopublisher_workbench-*-py3-none-any.whl' | sort)
if (( ${#wheels[@]} != 1 )); then
  echo "Missing or ambiguous GeoPublisher application wheel." >&2
  exit 3
fi
# Python from the host is provisioned separately. No dynamic downloads from
# PyPI are allowed, even if pip has an index in the user's global config.
python3.12 -m venv "$destination"
export PIP_NO_INDEX=1
export PIP_CONFIG_FILE=/dev/null
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONNOUSERSITE=1
if ! "$destination/bin/python" -m pip --version >/dev/null 2>&1; then
  echo "python3.12-venv/ensurepip unavailable; installed folder is INCOMPLETE." >&2
  exit 3
fi
"$destination/bin/python" -m pip install \
  --no-index --only-binary=:all: --find-links "$bundle/wheels" "${wheels[0]}"
"$destination/bin/geopub" --version
"$destination/bin/geopub" sheet-boundary-check --help >/dev/null
echo "OFFLINE_LINUX_CLI_INSTALL=COMPLETE"
echo "INSTALL_PATH=$destination"
echo "SYSTEM_GDAL_VERSION=$version"
