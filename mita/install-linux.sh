#!/usr/bin/env bash
# Install a supported client from its official GitHub Release.
# Usage: bash install-linux.sh <client-id> [auto|deb|rpm|appimage] [--dry-run]
#
# Supported clients and their release asset patterns come from clients.json, the same
# catalog the subscription page uses. It is read from next to this script when present,
# otherwise from the repository branch this script is published from.
set -euo pipefail

CATALOG_URL="${MITA_CLIENTS_URL:-https://raw.githubusercontent.com/lendysx16-new/mita-ha/main/mita/clients.json}"

usage() {
  printf 'Usage: bash install-linux.sh <client-id> [auto|deb|rpm|appimage] [--dry-run]\n' >&2
  exit 2
}

[[ $# -ge 1 && $# -le 3 ]] || usage
client="$1"
format="auto"
dry_run=""
if [[ $# -ge 2 ]]; then format="$2"; fi
if [[ $# -ge 3 ]]; then dry_run="$3"; fi
[[ "$client" =~ ^[a-z0-9-]+$ ]] || usage
[[ "$format" == "auto" || "$format" == "deb" || "$format" == "rpm" || "$format" == "appimage" ]] || usage
[[ -z "$dry_run" || "$dry_run" == "--dry-run" ]] || usage
command -v python3 >/dev/null || { printf 'Python 3 is required to resolve GitHub Release assets.\n' >&2; exit 1; }

machine="${MITA_INSTALL_ARCH:-$(uname -m)}"  # override is for tests and dry runs
case "$machine" in
  x86_64|amd64) arch="amd64" ;;
  aarch64|arm64) arch="arm64" ;;
  *) printf 'Unsupported Linux architecture: %s\n' "$machine" >&2; exit 1 ;;
esac

# Formats this machine can install, most native first. AppImage always works as a fallback.
preferred=""
if command -v apt-get >/dev/null && command -v dpkg >/dev/null; then preferred="deb"; fi
if command -v dnf >/dev/null || command -v zypper >/dev/null; then preferred="${preferred:+$preferred }rpm"; fi
preferred="${preferred:+$preferred }appimage"
preferred="${MITA_INSTALL_PREFER:-$preferred}"  # override is for tests and dry runs

temp_dir="$(mktemp -d)"
trap 'rm -rf -- "$temp_dir"' EXIT

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
catalog="$script_dir/clients.json"
if [[ ! -f "$catalog" ]]; then
  command -v curl >/dev/null || { printf 'curl is required to download the client catalog.\n' >&2; exit 1; }
  catalog="$temp_dir/clients.json"
  curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 --output "$catalog" "$CATALOG_URL"
fi

# Prints "repo|pattern|format" for the chosen client/arch/format, or explains what exists.
selection="$(python3 -I - "$catalog" "$client" "$arch" "$format" "$preferred" <<'PY'
import json
import sys

catalog, client_id, arch, requested, preferred = sys.argv[1:]
clients = {c["id"]: c for c in json.load(open(catalog, encoding="utf-8"))["clients"]}
client = clients.get(client_id)
if client is None:
    supported = sorted(c for c, v in clients.items() if any(d.get("os") == "linux" and "asset" in d for d in v["downloads"]))
    raise SystemExit(f"Unknown client '{client_id}'. Supported: {', '.join(supported)}")
assets = {
    d["target"]: d["asset"]
    for d in client["downloads"]
    if d.get("os") == "linux" and "asset" in d and d.get("target", "").startswith(f"linux-{arch}-")
}
available = [t.rsplit("-", 1)[1] for t in assets]
candidates = [requested] if requested != "auto" else preferred.split()
for fmt in candidates:
    target = f"linux-{arch}-{fmt}"
    if target in assets:
        print(f"{client['repo']}|{assets[target]}|{fmt}")
        break
else:
    have = ", ".join(available) or "none"
    raise SystemExit(
        f"No official {client['name']} release for {arch} / {'/'.join(candidates)}. Available for {arch}: {have}."
    )
PY
)" || exit 2
IFS='|' read -r repo pattern format <<<"$selection"

# Offline option for CI and for checking compatibility before installing.
if [[ "$dry_run" == "--dry-run" ]]; then
  printf '%s|%s|%s\n' "$repo" "$pattern" "$format"
  exit 0
fi

command -v curl >/dev/null || { printf 'curl is required to download release assets.\n' >&2; exit 1; }
if [[ "$format" == "deb" ]]; then
  command -v apt-get >/dev/null || { printf 'Apt is required for DEB; try RPM or AppImage instead.\n' >&2; exit 1; }
elif [[ "$format" == "rpm" ]]; then
  { command -v dnf >/dev/null || command -v zypper >/dev/null; } || {
    printf 'DNF or Zypper is required for RPM installs.\n' >&2; exit 1;
  }
fi

printf 'Fetching latest %s release (%s / %s)...\n' "$client" "$arch" "$format"
asset_url="$(python3 -I - "$repo" "$pattern" <<'PY'
import json
import re
import sys
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

repo, pattern = sys.argv[1:]
request = Request(
    f"https://api.github.com/repos/{repo}/releases/latest",
    headers={"Accept": "application/vnd.github+json", "User-Agent": "mita-ha-installer"},
)
try:
    with urlopen(request, timeout=20) as response:
        assets = json.load(response).get("assets", [])
except Exception as error:
    raise SystemExit(f"GitHub release lookup failed: {error}")
for asset in assets:
    if re.fullmatch(pattern, str(asset.get("name", ""))):
        url = str(asset.get("browser_download_url", ""))
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "github.com" or not parsed.path.startswith(f"/{repo}/releases/download/"):
            raise SystemExit("Unexpected release download URL")
        print(url)
        break
else:
    raise SystemExit("No matching asset found in latest GitHub Release")
PY
)"

case "$format" in
  deb) package_path="$temp_dir/$client.deb" ;;
  rpm) package_path="$temp_dir/$client.rpm" ;;
  appimage) package_path="$temp_dir/$client.AppImage" ;;
esac
curl --fail --location --silent --show-error --retry 2 --proto '=https' --tlsv1.2 \
  --output "$package_path" "$asset_url"
test -s "$package_path" || { printf 'Downloaded file is empty.\n' >&2; exit 1; }

case "$format" in
  deb)
    sudo apt-get install "$package_path"
    ;;
  rpm)
    if command -v dnf >/dev/null; then
      sudo dnf install "$package_path"
    else
      sudo zypper install "$package_path"
    fi
    ;;
  appimage)
    mkdir -p "$HOME/.local/bin"
    install -m 755 "$package_path" "$HOME/.local/bin/$client.AppImage"
    printf 'Installed to %s/.local/bin/%s.AppImage\n' "$HOME" "$client"
    printf 'Start it once so it can register its link handler, then return to the setup page and press "Open".\n'
    ;;
esac
printf '%s installation complete.\n' "$client"
