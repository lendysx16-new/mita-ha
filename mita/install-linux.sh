#!/usr/bin/env bash
# Install a supported client from its official GitHub Release.
# Usage: bash install-linux.sh <clashmi|karing|flclashx|koala> [auto|deb|rpm|appimage] [--dry-run]
set -euo pipefail

usage() {
  printf 'Usage: bash install-linux.sh <clashmi|karing|flclashx|koala> [auto|deb|rpm|appimage] [--dry-run]\n' >&2
  exit 2
}

[[ $# -ge 1 && $# -le 3 ]] || usage
client="$1"
format="auto"
dry_run=""
if [[ $# -ge 2 ]]; then format="$2"; fi
if [[ $# -ge 3 ]]; then dry_run="$3"; fi
[[ "$format" == "auto" || "$format" == "deb" || "$format" == "rpm" || "$format" == "appimage" ]] || usage
[[ -z "$dry_run" || "$dry_run" == "--dry-run" ]] || usage

case "$(uname -m)" in
  x86_64|amd64) arch="amd64" ;;
  aarch64|arm64) arch="arm64" ;;
  *) printf 'Unsupported Linux architecture: %s\n' "$(uname -m)" >&2; exit 1 ;;
esac

if [[ "$format" == "auto" ]]; then
  if command -v apt-get >/dev/null && command -v dpkg >/dev/null; then
    format="deb"
  elif command -v dnf >/dev/null || command -v zypper >/dev/null; then
    format="rpm"
  else
    format="appimage"
  fi
fi

# Match the official published release filename, never an arbitrary GitHub asset.
case "$client:$arch:$format" in
  clashmi:amd64:deb)      repo="KaringX/clashmi"; pattern='^clashmi_.*_linux_amd64\.deb$' ;;
  clashmi:amd64:rpm)      repo="KaringX/clashmi"; pattern='^clashmi_.*_linux_amd64\.rpm$' ;;
  clashmi:amd64:appimage) repo="KaringX/clashmi"; pattern='^clashmi_.*_linux_amd64\.AppImage$' ;;
  karing:amd64:deb)       repo="KaringX/karing"; pattern='^karing_.*_linux_amd64\.deb$' ;;
  karing:amd64:rpm)       repo="KaringX/karing"; pattern='^karing_.*_linux_amd64\.rpm$' ;;
  karing:amd64:appimage)  repo="KaringX/karing"; pattern='^karing_.*_linux_amd64\.AppImage$' ;;
  flclashx:amd64:deb)      repo="pluralplay/FlClashX"; pattern='^FlClashX-linux-amd64\.deb$' ;;
  flclashx:amd64:rpm)      repo="pluralplay/FlClashX"; pattern='^FlClashX-linux-amd64\.rpm$' ;;
  flclashx:amd64:appimage) repo="pluralplay/FlClashX"; pattern='^FlClashX-linux-amd64\.AppImage$' ;;
  flclashx:arm64:deb)      repo="pluralplay/FlClashX"; pattern='^FlClashX-linux-arm64\.deb$' ;;
  koala:amd64:deb) repo="coolcoala/koala-clash"; pattern='^Koala\.Clash_amd64\.deb$' ;;
  koala:arm64:deb) repo="coolcoala/koala-clash"; pattern='^Koala\.Clash_arm64\.deb$' ;;
  koala:amd64:rpm) repo="coolcoala/koala-clash"; pattern='^Koala\.Clash_x86_64\.rpm$' ;;
  koala:arm64:rpm) repo="coolcoala/koala-clash"; pattern='^Koala\.Clash_aarch64\.rpm$' ;;
  *)
    printf 'No official %s release for %s / %s. Choose a supported client or package format.\n' "$client" "$arch" "$format" >&2
    exit 2
    ;;
esac

# Offline option for CI and for checking compatibility before installing.
if [[ "$dry_run" == "--dry-run" ]]; then
  printf '%s|%s\n' "$repo" "$pattern"
  exit 0
fi

command -v python3 >/dev/null || { printf 'Python 3 is required to resolve GitHub Release assets.\n' >&2; exit 1; }
command -v curl >/dev/null || { printf 'curl is required to download release assets.\n' >&2; exit 1; }
if [[ "$format" == "deb" ]]; then
  command -v apt-get >/dev/null || { printf 'Apt is required for DEB; try RPM or AppImage instead.\n' >&2; exit 1; }
elif [[ "$format" == "rpm" ]]; then
  { command -v dnf >/dev/null || command -v zypper >/dev/null; } || {
    printf 'DNF or Zypper is required for RPM installs.\n' >&2; exit 1;
  }
fi

printf 'Fetching latest %s release (%s / %s)...\n' "$client" "$arch" "$format"
asset_url="$(python3 - "$repo" "$pattern" <<'PY'
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

temp_dir="$(mktemp -d)"
trap 'rm -rf -- "$temp_dir"' EXIT
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
    ;;
esac
printf '%s installation complete.\n' "$client"
