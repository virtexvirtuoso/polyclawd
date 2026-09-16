/* whale-hub.js — suite overview strip for whale.html (the hub).
 *
 * Injected only on the hub page, right under its <header class="chrome">.
 * Uses the host page's own CSS variables (--mono/--disp/--panel/--line/...)
 * so it is native to the WHALE//SHARK design system by construction.
 * Four live module cards route to the rest of the suite.
 */
(function () {
  'use strict';
  var API = '/polyclawd/api';

  var css = [
    '#whale-hub{display:grid;grid-template-columns:repeat(4,1fr);gap:0;',
    ' border-bottom:1px solid var(--line);background:var(--bg2,#0d0e15)}',
    '#whale-hub a{display:block;padding:12px 18px 11px;text-decoration:none;',
    ' border-right:1px solid var(--line);position:relative;transition:background .15s}',
    '#whale-hub a:last-child{border-right:none}',
    '#whale-hub a::after{content:"";position:absolute;left:0;right:0;bottom:0;height:2px;',
    ' background:var(--low-tint,#4ecdc4);transform:scaleX(0);transition:transform .2s;transform-origin:left}',
    '#whale-hub a:hover{background:var(--panel,#0e0f16)}',
    '#whale-hub a:hover::after{transform:scaleX(1)}',
    '#whale-hub .hk{font-family:var(--mono);font-size:9px;letter-spacing:.22em;',
    ' text-transform:uppercase;color:var(--faint,#4d5268)}',
    '#whale-hub .hk em{font-style:normal;color:var(--low-tint,#4ecdc4)}',
    '#whale-hub .hv{font-family:var(--disp);font-size:26px;font-weight:800;line-height:1;',
    ' color:var(--ink,#e7eaf2);margin-top:5px;letter-spacing:.02em}',
    '#whale-hub .hv.pos{color:var(--bid,#00d68f)}',
    '#whale-hub .hv.neg{color:var(--ask,#f65164)}',
    '#whale-hub .hs{font-family:var(--mono);font-size:9.5px;color:var(--dim,#8d93a8);margin-top:4px}',
    '@media(max-width:900px){#whale-hub{grid-template-columns:repeat(2,1fr)}',
    ' #whale-hub a:nth-child(2){border-right:none}}',
  ].join('');

  var style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  var hub = document.createElement('div');
  hub.id = 'whale-hub';
  hub.innerHTML =
    '<a href="whale-flow.html"><span class="hk"><em>01</em> FLOW · live map</span>' +
    '<div class="hv" id="hub-flow">&mdash;</div><div class="hs" id="hub-flow-s">alert flow, dollar-weighted</div></a>' +
    '<a href="whale-follow.html"><span class="hk"><em>02</em> FOLLOWS · paper strategy</span>' +
    '<div class="hv" id="hub-pnl">&mdash;</div><div class="hs" id="hub-pnl-s">net of fees + slippage</div></a>' +
    '<a href="whales.html"><span class="hk"><em>03</em> WALLETS · smart ledger</span>' +
    '<div class="hv" id="hub-wal">&mdash;</div><div class="hs" id="hub-wal-s">proven winners tracked</div></a>' +
    '<a href="whale-arch.html"><span class="hk"><em>04</em> PIPELINE · how it works</span>' +
    '<div class="hv" id="hub-pipe">&mdash;</div><div class="hs" id="hub-pipe-s">sweep → books → score → follow</div></a>';

  var chrome = document.querySelector('header.chrome');
  if (chrome && chrome.parentNode) {
    chrome.parentNode.insertBefore(hub, chrome.nextSibling);
  } else {
    document.body.insertBefore(hub, document.body.firstChild);
  }

  function txt(id, v, cls) {
    var e = document.getElementById(id);
    if (!e) return;
    e.innerHTML = v;
    if (cls !== undefined) e.className = 'hv ' + cls;
  }
  function sub(id, v) {
    var e = document.getElementById(id);
    if (e) e.textContent = v;
  }
  function money(v) {
    var s = v < 0 ? '-$' : '$';
    v = Math.abs(v);
    return s + (v >= 1e6 ? (v / 1e6).toFixed(1) + 'M'
              : v >= 1e3 ? (v / 1e3).toFixed(1) + 'K' : v.toFixed(0));
  }

  function refresh() {
    fetch(API + '/whale/stats').then(function (r) { return r.json(); })
      .then(function (d) {
        var a24 = (d.health && d.health.alerts_24h) || 0;
        txt('hub-flow', a24.toLocaleString());
        var c = d.counts_24h || {};
        sub('hub-flow-s', (c.CRITICAL || 0) + ' crit · ' + (c.HIGH || 0) + ' high · 24h');
        var age = d.health && d.health.last_scan_age_s;
        var mk = d.health && d.health.markets_tracked;
        var n = 0;
        if (mk) for (var k in mk) n += mk[k];
        txt('hub-pipe', age == null ? '&mdash;'
          : age < 120 ? age + 's' : Math.round(age / 60) + 'm', age != null && age > 900 ? 'neg' : '');
        sub('hub-pipe-s', 'last sweep · ' + n.toLocaleString() + ' markets watched');
      }).catch(function () {});
    fetch(API + '/whale/follows?limit=1').then(function (r) { return r.json(); })
      .then(function (d) {
        var t = d.totals || {};
        var pnl = t.pnl_net || 0;
        txt('hub-pnl', money(pnl), pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '');
        sub('hub-pnl-s', (t.open || 0) + ' open · ' + (t.closed || 0) + ' round-trips · paper');
      }).catch(function () {});
    fetch(API + '/whale/wallets?limit=1').then(function (r) { return r.json(); })
      .then(function (d) {
        txt('hub-wal', (d.smart_count || 0) + '<span style="font-size:14px;color:var(--faint)"> smart</span>');
        sub('hub-wal-s', (d.tracked || 0).toLocaleString() + ' tracked · ' +
            (d.queued || 0).toLocaleString() + ' queued');
      }).catch(function () {});
  }
  refresh();
  setInterval(function () { if (!document.hidden) refresh(); }, 60000);
})();
