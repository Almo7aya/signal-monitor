#!/bin/sh
# Copy signal-monitor to the OpenWrt gateway and run install.sh there (needs ssh key access as root).
#   ./deploy.sh                       # root@$SIGNALMON_ROUTER, else root@192.168.1.1
#   ./deploy.sh root@10.0.1.1
set -e
ROUTER=${1:-${SIGNALMON_ROUTER:-root@192.168.1.1}}
cd "$(dirname "$0")"
tar -cf - files install.sh uninstall.sh |
	ssh -o BatchMode=yes -o ConnectTimeout=8 "$ROUTER" \
		'rm -rf /tmp/signalmon-src && mkdir /tmp/signalmon-src && tar -xf - -C /tmp/signalmon-src && sh /tmp/signalmon-src/install.sh'
