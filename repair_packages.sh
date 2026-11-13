#!/usr/bin/env bash
set -euo pipefail

# Recover an interrupted Ubuntu/PackageKit update.  This uses already cached
# packages when available; apt only accesses the network if a required archive
# is missing locally.
if [[ "${EUID}" -ne 0 ]]; then
  exec sudo "$0" "$@"
fi

export DEBIAN_FRONTEND=noninteractive

echo "[repair] configuring unpacked packages"
dpkg --configure -a

echo "[repair] fixing package dependencies"
apt-get -y --fix-broken install

# PackageKit can leave one member of this tightly coupled package family at an
# older revision even after --fix-broken succeeds.  Install the whole family in
# one transaction so desktop icon loaders use one ABI-compatible version.
echo "[repair] aligning libgdk-pixbuf package versions"
apt-get -y install \
  libgdk-pixbuf-2.0-0 \
  libgdk-pixbuf2.0-common \
  libgdk-pixbuf2.0-bin

echo "[repair] verifying package state"
apt-get check
dpkg --audit

pixbuf_versions="$(dpkg-query -W -f='${Version}\n' \
  libgdk-pixbuf-2.0-0 libgdk-pixbuf2.0-common libgdk-pixbuf2.0-bin | sort -u)"
if [[ "$(printf '%s\n' "$pixbuf_versions" | sed '/^$/d' | wc -l)" -ne 1 ]]; then
  echo "[repair] ERROR: libgdk-pixbuf versions are still inconsistent:" >&2
  printf '%s\n' "$pixbuf_versions" >&2
  exit 1
fi
echo "[repair] libgdk-pixbuf version: $pixbuf_versions"

if [[ -f /var/run/reboot-required ]]; then
  echo "[repair] complete; reboot required"
  if [[ -f /var/run/reboot-required.pkgs ]]; then
    sed -n '1,80p' /var/run/reboot-required.pkgs
  fi
else
  echo "[repair] complete; reboot not required"
fi
