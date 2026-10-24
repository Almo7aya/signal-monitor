#!/bin/sh
# Remove signal-monitor from the gateway. Keeps the data directory (snapshots, speed-test history).
#   sh uninstall.sh            keep /etc/config/signalmon
#   sh uninstall.sh --purge    also delete /etc/config/signalmon
DATA=$(uci -q get signalmon.main.data_dir || echo /root/signalmon)
/etc/init.d/signalmon stop 2>/dev/null
/etc/init.d/signalmon disable 2>/dev/null
rm -rf /usr/share/signalmon /www/luci-static/resources/view/signalmon /var/run/signalmon
rm -f /etc/init.d/signalmon /usr/libexec/rpcd/signalmon /usr/share/rpcd/acl.d/luci-app-signalmon.json \
	/usr/share/luci/menu.d/luci-app-signalmon.json
[ "$1" = --purge ] && rm -f /etc/config/signalmon
sed -i '\#^/usr/share/signalmon/$#d; \#^/etc/init.d/signalmon$#d; \#^/usr/libexec/rpcd/signalmon$#d; \#luci-app-signalmon\.json$#d; \#^/www/luci-static/resources/view/signalmon/$#d' /etc/sysupgrade.conf
/etc/init.d/rpcd reload
rm -f /tmp/luci-indexcache*
rm -rf /tmp/luci-modulecache
echo "signal-monitor removed (data kept in $DATA)"
