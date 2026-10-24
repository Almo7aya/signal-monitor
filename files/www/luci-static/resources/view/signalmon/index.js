'use strict';
'require view';
'require form';
'require rpc';
'require ui';
'require poll';

// Services > Signal Monitor: turn the monitor on/off (it polls both routers only while on), see how long until it
// turns itself off, open it, and edit /etc/config/signalmon.

var callStatus = rpc.declare({ object: 'signalmon', method: 'status' });
var callStart = rpc.declare({ object: 'signalmon', method: 'start' });
var callStop = rpc.declare({ object: 'signalmon', method: 'stop' });
var callExtend = rpc.declare({ object: 'signalmon', method: 'extend', params: [ 'minutes' ] });

var CSS = [
	'.sm-card{display:flex;flex-wrap:wrap;align-items:center;gap:12px 20px;padding:14px 16px;border-radius:10px;',
	'border:1px solid rgba(128,128,128,.3);margin-bottom:1em}',
	'.sm-state{display:flex;align-items:center;gap:10px;font-size:110%;min-width:16em}',
	'.sm-dot{width:12px;height:12px;border-radius:50%;background:#898781;flex:none}',
	'.sm-dot.on{background:#0ca30c;box-shadow:0 0 0 4px rgba(12,163,12,.18)}',
	'.sm-switch{position:relative;width:52px;height:28px;border-radius:14px;border:0;cursor:pointer;flex:none;',
	'background:rgba(128,128,128,.45);transition:background .15s;padding:0}',
	'.sm-switch::after{content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;',
	'background:#fff;transition:left .15s;box-shadow:0 1px 3px rgba(0,0,0,.3)}',
	'.sm-switch[aria-checked="true"]{background:#0ca30c}.sm-switch[aria-checked="true"]::after{left:27px}',
	'.sm-switch:disabled{opacity:.5;cursor:wait}',
	'.sm-sub{opacity:.75;font-size:90%}',
	'.sm-actions{display:flex;flex-wrap:wrap;gap:8px;margin-left:auto}',
	'.sm-frame{width:100%;height:80vh;border:1px solid rgba(128,128,128,.3);border-radius:8px;background:#0d0d0d}'
].join('');

function mins(s) {
	return s < 90 ? _('%d s').format(s) : s < 5400 ? _('%d min').format(Math.round(s / 60)) : _('%.1f h').format(s / 3600);
}

function hhmm(ts) {
	var d = new Date(ts * 1000), p = function(n) { return (n < 10 ? '0' : '') + n; };
	return p(d.getHours()) + ':' + p(d.getMinutes());
}

return view.extend({
	load: function() {
		return L.resolveDefault(callStatus(), {});
	},

	renderStatus: function(st) {
		var self = this, on = !!st.running;
		var sw = E('button', {
			'class': 'sm-switch', 'role': 'switch', 'aria-checked': on ? 'true' : 'false',
			'title': on ? _('Turn the signal monitor off') : _('Turn the signal monitor on'),
			'click': function(ev) {
				ev.target.disabled = true;
				return (on ? callStop() : callStart()).then(function(s) { self.update(s); })
					.catch(function(e) { ui.addNotification(null, E('p', e.message || String(e)), 'danger'); self.refresh(); });
			}
		});

		var line, sub = [];
		if (on) {
			line = E('span', {}, [ E('strong', {}, [ _('On') ]), ' — ' + _('polling both routers') ]);
			if (st.started)
				sub.push(_('since %s').format(hhmm(st.started)));
			sub.push(st.off_at ? _('turns itself off in %s (at %s)').format(mins(Math.max(0, st.off_at - st.now)), hhmm(st.off_at))
				: _('stays on until you turn it off'));
		}
		else {
			line = E('span', {}, [ E('strong', {}, [ _('Off') ]), ' — ' + _('the routers are not polled') ]);
			sub.push(st.auto_off > 0 ? _('when turned on, it turns itself off after %s').format(mins(st.auto_off * 60))
				: _('when turned on, it stays on until you turn it off'));
		}

		var actions = E('div', { 'class': 'sm-actions' });
		if (on) {
			actions.appendChild(E('a', { 'class': 'cbi-button cbi-button-action', 'href': st.url, 'target': '_blank', 'rel': 'noopener' },
				[ _('Open monitor') + ' ↗' ]));
			if (st.off_at)
				actions.appendChild(E('button', { 'class': 'cbi-button', 'click': function(ev) {
					ev.target.disabled = true;
					return callExtend(st.auto_off > 0 ? +st.auto_off : 60).then(function(s) { self.update(s); });
				} }, [ _('Restart the timer') ]));
		}

		return E('div', { 'class': 'sm-card' }, [
			sw,
			E('div', {}, [
				E('div', { 'class': 'sm-state' }, [ E('span', { 'class': 'sm-dot' + (on ? ' on' : '') }), line ]),
				E('div', { 'class': 'sm-sub' }, [ sub.join(' · ') ])
			]),
			actions
		]);
	},

	update: function(st) {
		this.status = st || {};
		if (this.statusNode)
			this.statusNode.replaceChildren(this.renderStatus(this.status));
		if (this.frameBox) {
			var running = !!this.status.running;
			this.frameBox.style.display = running ? '' : 'none';
			if (!running && this.frameBox.open) this.frameBox.open = false;
		}
	},

	refresh: function() {
		var self = this;
		return L.resolveDefault(callStatus(), {}).then(function(s) { self.update(s); });
	},

	render: function(st) {
		var self = this, m, s, o;
		if (!document.getElementById('sm-css'))
			document.head.appendChild(E('style', { 'id': 'sm-css' }, [ CSS ]));

		this.statusNode = E('div');
		var frame = E('iframe', { 'class': 'sm-frame', 'title': _('Signal monitor') });
		this.frameBox = E('details', { 'toggle': function(ev) {
			if (ev.target.open && !frame.getAttribute('src')) frame.setAttribute('src', self.status.url);
			if (!ev.target.open) frame.removeAttribute('src');  // stop the page polling when hidden
		} }, [ E('summary', { 'style': 'cursor:pointer' }, [ _('Show the monitor on this page') ]), frame ]);
		this.update(st);
		poll.add(L.bind(this.refresh, this), 5);

		m = new form.Map('signalmon', _('Signal Monitor'),
			_('Live LTE signal of both 4G routers (per antenna, neighbour cells, bandwidth per WAN, speed tests, ' +

			  'snapshots). It polls the routers\' modems only while it is on, so turn it on when you need it.'));

		s = m.section(form.NamedSection, 'main', 'signalmon', _('General'));
		s.addremove = false;

		o = s.option(form.ListValue, 'auto_off', _('Turn off automatically'),
			_('Stops polling the routers after this long. "Restart the timer" above starts it again from now.'));
		[ [ '15', _('after 15 min') ], [ '30', _('after 30 min') ], [ '60', _('after 1 hour') ], [ '120', _('after 2 hours') ],
		  [ '240', _('after 4 hours') ], [ '480', _('after 8 hours') ], [ '0', _('never') ] ]
			.forEach(function(v) { o.value(v[0], v[1]); });
		o.default = '60';

		o = s.option(form.Value, 'listen', _('Listen address'), _('Address the monitor page is served on. Empty: the LAN address.'));
		o.datatype = 'ip4addr("nomask")';
		o.placeholder = _('LAN address');

		o = s.option(form.Value, 'port', _('Port'));
		o.datatype = 'port';
		o.placeholder = '8765';

		o = s.option(form.Value, 'data_dir', _('Data directory'), _('Snapshots, speed-test history and spot/speed CSV (kept).'));
		o.placeholder = '/root/signalmon';

		o = s.option(form.Value, 'log_dir', _('Log directory'),
			_('Per-poll CSV logs. /tmp is RAM: spares the SD card, cleared on reboot.'));
		o.placeholder = '/tmp/signalmon';

		o = s.option(form.Value, 'speed_url', _('Speed-test URL'), _('Downloaded through each WAN by the speed-test buttons (uses mobile data).'));
		o.placeholder = 'https://speed.cloudflare.com/__down?bytes=50000000';

		o = s.option(form.Value, 'speed_seconds', _('Speed-test length (s)'));
		o.datatype = 'range(3,60)';
		o.placeholder = '10';

		// the mwan3 interfaces each router serves: bandwidth cards and speed tests
		var wanOptions = function(s, dflt) {
			var o = s.option(form.DynamicList, 'wans', _('WANs'), _('The gateway\'s mwan3 interfaces that go through this router (one per PDN).'));
			o.placeholder = dflt;
			o = s.option(form.Value, 'ping_wan', _('Speed-test ping WAN'), _('Pinged during this router\'s speed tests (latency under load).'));
			o.placeholder = dflt;
		};

		s = m.section(form.NamedSection, 'old', 'router', _('Old router (ZTE ZX297520V3)'));
		s.addremove = false;
		o = s.option(form.Value, 'name', _('Name'));
		o.placeholder = _('Old router');
		o = s.option(form.Value, 'host', _('Address'));
		o.datatype = 'host';
		o = s.option(form.Value, 'port', _('Telnet port'));
		o.datatype = 'port';
		o = s.option(form.ListValue, 'poll', _('Poll every'),
			_('With AT commands it rebooted when polled every second; the monitor never goes below 2 s with AT on.'));
		[ [ '2', _('2 s') ], [ '3', _('3 s') ], [ '5', _('5 s') ], [ '10', _('10 s') ] ].forEach(function(v) { o.value(v[0], v[1]); });
		o.default = '2';
		o = s.option(form.Flag, 'at', _('Live values via AT'),
			_('One AT command per poll for live RSRP/RSRQ and per-port RSSI. Off = firmware values only (refreshed every ~3 s), the safest.'));
		o.default = '1';
		o.rmempty = false;
		wanOptions(s, 'wan2');

		s = m.section(form.NamedSection, 'new', 'router', _('New router (SIMCom SIM7906E)'));
		s.addremove = false;
		o = s.option(form.Value, 'name', _('Name'));
		o.placeholder = _('New router');
		o = s.option(form.Value, 'host', _('Address'));
		o.datatype = 'host';
		o = s.option(form.Value, 'port', _('Telnet port'));
		o.datatype = 'port';
		o = s.option(form.Value, 'user', _('Telnet user'));
		o = s.option(form.Value, 'password', _('Telnet password'));
		o.password = true;
		o = s.option(form.ListValue, 'poll', _('Poll every'));
		[ [ '1', _('1 s') ], [ '2', _('2 s') ], [ '3', _('3 s') ], [ '5', _('5 s') ] ].forEach(function(v) { o.value(v[0], v[1]); });
		o.default = '1';
		o = s.option(form.Value, 'neighbour_every', _('Neighbour cells every (s)'));
		o.datatype = 'range(5,600)';
		o.placeholder = '30';
		wanOptions(s, 'wan1');

		return m.render().then(function(mapNode) {
			// on/off card and the embedded page go between the map's description and its settings sections
			var top = E('div', {}, [
				self.statusNode,
				self.frameBox,
				E('p', { 'class': 'sm-sub', 'style': 'margin:.5em 0 1.5em' }, [
					_('Saving the settings below restarts the monitor if it is on (and restarts its auto-off timer).') ])
			]);
			mapNode.insertBefore(top, mapNode.querySelector('.cbi-section'));
			return mapNode;
		});
	}
});
