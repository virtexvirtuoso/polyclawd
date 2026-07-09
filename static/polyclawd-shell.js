/* polyclawd-shell.js — the Probability Bureau site chrome.
 *
 * Adopt on any page (with css/polyclawd.css) via:
 *   <link rel="stylesheet" href="css/polyclawd.css?v=1">
 *   <script src="polyclawd-shell.js" defer></script>
 *
 * Injects the sticky bureau header (brand + section + site nav + mode
 * stamp) at the top of <body>, marks the active section, and tags
 * <body class="pc-shell-on"> so the legacy bridge can retire old navs.
 * Optional: <body data-pc-section="DESK NAME"> overrides the section
 * label (defaults from the nav match, then document.title).
 *
 * Multi-page sections carry a 4th NAV field — a [href,label] sub-list —
 * rendered as a hover/focus dropdown so every sub-view is reachable from
 * the top bar (replaces the old per-page in-page nav rows + whale dock).
 */
(function () {
  'use strict';

  // [primary href, label, [pages that mark this section active], [optional sub-items]]
  var NAV = [
    ['./',             'Desk',      ['index.html', '']],
    ['markets.html',   'Markets',   ['markets.html', 'arb.html', 'cross-arb.html', 'trade.html', 'poly-paper.html'],
        [['markets.html', 'Browser'], ['arb.html', 'Arb'], ['cross-arb.html', 'Cross-Arb'], ['trade.html', 'Trade'], ['poly-paper.html', 'Paper Poly']]],
    ['baseball.html',  'Sports',    ['baseball.html', 'soccer.html', 'ufc.html'],
        [['baseball.html', 'Baseball'], ['soccer.html', 'Soccer'], ['ufc.html', 'UFC']]],
    ['weather.html',   'Weather',   ['weather.html']],
    ['options.html',   'Options',   ['options.html']],
    ['elections.html', 'Elections', ['elections.html']],
    ['whale.html',     'Whales',    ['whale.html', 'whales.html', 'whale-flow.html', 'whale-follow.html', 'whale-arch.html', 'whale-tape.html', 'whale-wallets.html'],
        [['whale.html', 'Surveillance'], ['whale-flow.html', 'Flow'], ['whale-tape.html', 'Tape'], ['whales.html', 'Ledger'], ['whale-wallets.html', 'Atlas'], ['whale-follow.html', 'Follow'], ['whale-arch.html', 'Pipeline']]],
    ['portfolio.html', 'Portfolio', ['portfolio.html', 'analysis.html', 'live.html'],
        [['portfolio.html', 'Portfolio'], ['analysis.html', 'Analysis'], ['live.html', 'Live']]],
  ];

  var here = location.pathname.split('/').pop() || 'index.html';

  function esc(s) {
    return String(s).replace(/[<>&"]/g, function (c) {
      return { '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c];
    });
  }

  var chrome = document.createElement('header');
  chrome.className = 'pc-chrome';

  var section = document.body.getAttribute('data-pc-section') || '';
  var links = '';
  for (var i = 0; i < NAV.length; i++) {
    var item = NAV[i];
    var on = item[2].indexOf(here) !== -1;
    if (on && !section) section = item[1];
    var sub = item[3];
    if (sub && sub.length) {
      var subHtml = '';
      for (var j = 0; j < sub.length; j++) {
        var sOn = sub[j][0] === here ? ' class="on"' : '';
        subHtml += '<a href="' + sub[j][0] + '"' + sOn + '>' + esc(sub[j][1]) + '</a>';
      }
      links += '<span class="pc-navitem' + (on ? ' on' : '') + '">' +
                 '<a href="' + item[0] + '"' + (on ? ' class="on"' : '') + '>' +
                   esc(item[1]) + ' <i class="pc-caret">▾</i></a>' +
                 '<span class="pc-dropdown">' + subHtml + '</span>' +
               '</span>';
    } else {
      links += '<a href="' + item[0] + '"' + (on ? ' class="on"' : '') + '>' + esc(item[1]) + '</a>';
    }
  }
  if (!section) section = (document.title.split(/[|—–-]/)[0] || 'desk').trim();

  chrome.innerHTML =
    '<a class="pc-brand" href="./">' +
      '<b>POLY<i>//</i>CLAWD</b>' +
      '<span class="pc-sect">' + section.replace(/[<>&]/g, '') + '</span>' +
    '</a>' +
    '<nav class="pc-nav" aria-label="Polyclawd">' + links + '</nav>';

  document.body.insertBefore(chrome, document.body.firstChild);
  document.body.classList.add('pc-shell-on');
})();
