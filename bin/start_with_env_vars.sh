#!/usr/bin/env bash

set -euo pipefail

if [[ -n ${1:-} ]]; then
    export APP_CONFIG_FILE="$1"
fi

if command -v ip >/dev/null 2>&1; then
  lan_ips_csv="$(
    ip -o -4 addr show up 2>/dev/null \
    | awk '{split($4, a, "/"); if (a[1] != "" && a[1] != "127.0.0.1") print a[1]}' \
    | paste -sd, -
  )"
else
  echo "Error: 'ip' not found." >&2
  exit 1
fi

export MTX_WEBRTCADDITIONALHOSTS="${lan_ips_csv}"
export MTX_WEBRTCLOCALTCPADDRESS=":8189"

export TZ=$(readlink /etc/localtime | sed 's#^.*/zoneinfo/##')

docker compose up --build -d

if [[ -n "${MTX_WEBRTCADDITIONALHOSTS}" ]]; then
   echo "MediaMTX WebRTC additional hosts: ${MTX_WEBRTCADDITIONALHOSTS}"
else
   echo "MediaMTX WebRTC additional hosts: none detected"
fi
