#!/usr/bin/env bash

set -euo pipefail

lan_ips_csv=""

if command -v ip >/dev/null 2>&1; then
   lan_ips_csv="$(
      ip -o -4 addr show up scope global \
      | awk '{split($4, a, "/"); print a[1]}'
      | paste -sd, -
   )"
elif command -v ifconfig >/dev/null 2>&1; then
   lan_ips_csv="$(
      ifconfig \
      | awk '
            /^[^ \t]/ {
            if (iface != "" && active && ip != "" && ip != "127.0.0.1") {
               print ip
            }
            iface = $1
            sub(/:$/, "", iface)
            active = 0
            ip = ""
            }
            /inet / && $2 != "127.0.0.1" {
            ip = $2
            }
            /status: active/ {
            active = 1
            }
            END {
            if (iface != "" && active && ip != "" && ip != "127.0.0.1") {
               print ip
            }
            }
         ' \
      | paste -sd, -
   )"
fi

export MTX_WEBRTCADDITIONALHOSTS="${lan_ips_csv}"
export MTX_WEBRTCLOCALTCPADDRESS=":8189"

if [[ -n "${MTX_WEBRTCADDITIONALHOSTS}" ]]; then
   echo "MediaMTX WebRTC additional hosts: ${MTX_WEBRTCADDITIONALHOSTS}"
else
   echo "MediaMTX WebRTC additional hosts: none detected"
fi

docker compose up --build -d