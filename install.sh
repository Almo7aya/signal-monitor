#!/bin/sh
# Install (or update) signal-monitor on the OpenWrt gateway. Run from the unpacked project directory:
#   sh install.sh
# Keeps an existing /etc/config/signalmon (your settings and router password). If the monitor is on, it is
# restarted so the new code runs; otherwise it stays off.
set -e
cd "$(dirname "$0")"
find files -type f | while read -r f; do sed -i 's/\r$//' "$f"; done  # in case of a Windows checkout

find files -type f | while read -r f; do
	dst="/${f#files/}"
	if [ "$dst" = /etc/config/signalmon ] && [ -f "$dst" ]; then
		echo "keeping $dst"
		continue
	fi
	mkdir -p "$(dirname "$dst")"
	cp "$f" "$dst"
done
chmod 755 /etc/init.d/signalmon /usr/libexec/rpcd/signalmon /usr/share/signalmon/signalmon.py
mkdir -p "$(uci -q get signalmon.main.data_dir || echo /root/signalmon)"

for f in /usr/share/signalmon/ /etc/init.d/signalmon /usr/libexec/rpcd/signalmon \
	/usr/share/rpcd/acl.d/luci-app-signalmon.json /usr/share/luci/menu.d/luci-app-signalmon.json \
	/www/luci-static/resources/view/signalmon/ "$(uci -q get signalmon.main.data_dir || echo /root/signalmon)/"; do
	grep -qxF "$f" /etc/sysupgrade.conf || echo "$f" >> /etc/sysupgrade.conf
done

/etc/init.d/signalmon enable  # boot: starts only if it was left on (enabled=1)
/etc/init.d/rpcd reload
sleep 1
ubus list signalmon >/dev/null 2>&1 || { /etc/init.d/rpcd restart; sleep 2; }
rm -f /tmp/luci-indexcache*
rm -rf /tmp/luci-modulecache

if [ "$(uci -q get signalmon.main.enabled)" = 1 ]; then
	/etc/init.d/signalmon restart
	echo "signal-monitor updated and restarted"
else
	echo "signal-monitor installed (off). Turn it on in LuCI: Services > Signal Monitor, or: ubus call signalmon start"
fi
[ "$(uci -q get signalmon.new.password)" = CHANGE_ME ] && echo "note: set the new router's telnet password: uci set signalmon.new.password=...; uci commit signalmon"
exit 0
