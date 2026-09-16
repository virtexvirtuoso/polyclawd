/* whale-nav.js — shared floating dock tying the whale suite together.
 *
 * Include on every whale page:  <script src="whale-nav.js" defer></script>
 * Self-contained: injects its own styles, detects the active page, and
 * shows live status (scan health, today's CRITICALs, paper P&L) so every
 * page carries the same spine. No dependencies, no layout assumptions —
 * it floats above whatever design the host page uses.
 */
(function () {
  'use strict';
  var API = '/polyclawd/api';
  var PAGES = [
    ['whale-flow.html',   'FLOW'],
    ['whale.html',        'ALERTS'],
    ['whale-follow.html', 'FOLLOWS'],
    ['whales.html',       'WALLETS'],
    ['whale-arch.html',   'PIPELINE'],
  ];
  var here = (location.pathname.split('/').pop() || 'whale.html');

  var css = [
    '#whale-dock{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);',
    ' z-index:9999;display:flex;align-items:center;gap:2px;padding:6px 10px;',
    ' background:rgba(11,12,18,.9);border:1px solid #272b40;border-radius:999px;',
    ' backdrop-filter:blur(10px);box-shadow:0 8px 32px rgba(0,0,0,.55);',
    " font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;",
    ' letter-spacing:.12em;white-space:nowrap}',
    '#whale-dock a{color:#8d93a8;text-decoration:none;padding:5px 11px;',
    ' border-radius:999px;transition:all .15s;text-transform:uppercase}',
    '#whale-dock a:hover{color:#e7eaf2;background:rgba(78,205,196,.08)}',
    '#whale-dock a.on{color:#4ecdc4;background:rgba(78,205,196,.13)}',
    '#whale-dock a.shark{font-size:13px;padding:0 6px 0 2px;background:none}',
    '#whale-dock .sep{width:1px;height:14px;background:#272b40;margin:0 6px}',
    '#whale-dock .pill{color:#4d5268;padding:0 7px;display:flex;align-items:center;gap:5px}',
    '#whale-dock .pill b{color:#e7eaf2;font-weight:600}',
    '#whale-dock .pill b.crit{color:#ff3b52}',
    '#whale-dock .pill b.pos{color:#00d68f}',
    '#whale-dock .pill b.neg{color:#f65164}',
    '#whale-dock .dot{width:7px;height:7px;border-radius:50%;background:#4ecdc4;',
    ' box-shadow:0 0 6px #4ecdc4}',
    '#whale-dock .dot.stale{background:#ff3b52;box-shadow:0 0 6px #ff3b52}',
    '@media (max-width:760px){#whale-dock .pill,#whale-dock .sep{display:none}}',
  ].join('');

  var style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  var dock = document.createElement('nav');
  dock.id = 'whale-dock';
  dock.setAttribute('aria-label', 'Whale suite navigation');

  var html = '<a class="shark" href="whale.html" title="hub">🦈</a>';
  for (var i = 0; i < PAGES.length; i++) {
    var on = PAGES[i][0] === here ? ' class="on"' : '';
    html += '<a href="' + PAGES[i][0] + '"' + on + '>' + PAGES[i][1] + '</a>';
  }
  html += '<span class="sep"></span>' +
          '<span class="pill"><span class="dot" id="wd-dot"></span></span>' +
          '<span class="pill">crit <b class="crit" id="wd-crit">–</b></span>' +
          '<span class="pill">paper <b id="wd-pnl">–</b></span>';
  dock.innerHTML = html;
  document.body.appendChild(dock);

  function money(v) {
    var s = v < 0 ? '-$' : '$';
    v = Math.abs(v);
    return s + (v >= 1000 ? (v / 1000).toFixed(1) + 'K' : v.toFixed(0));
  }

  function refresh() {
    fetch(API + '/whale/stats').then(function (r) { return r.json(); })
      .then(function (d) {
        var age = d.health && d.health.last_scan_age_s;
        var dot = document.getElementById('wd-dot');
        if (dot) dot.className = 'dot' + ((age == null || age > 900) ? ' stale' : '');
        var crit = (d.counts_24h && d.counts_24h.CRITICAL) || 0;
        var el = document.getElementById('wd-crit');
        if (el) el.textContent = crit;
      }).catch(function () {});
    fetch(API + '/whale/follows?limit=1').then(function (r) { return r.json(); })
      .then(function (d) {
        var pnl = (d.totals && d.totals.pnl_net) || 0;
        var el = document.getElementById('wd-pnl');
        if (el) {
          el.textContent = money(pnl);
          el.className = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '';
        }
      }).catch(function () {});
  }
  refresh();
  setInterval(function () { if (!document.hidden) refresh(); }, 60000);
})();
