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
 */
(function () {
  'use strict';

  var NAV = [
    ['./',                 'Desk',     ['index.html', '']],
    ['markets.html',       'Markets',  ['markets.html', 'arb.html', 'cross-arb.html', 'trade.html', 'poly-paper.html']],
    ['baseball.html',      'Sports',   ['baseball.html', 'soccer.html', 'ufc.html']],
    ['weather.html',       'Weather',  ['weather.html']],
    ['options.html',       'Options',  ['options.html']],
    ['elections.html',     'Elections',['elections.html']],
    ['whale.html',         'Whales',   ['whale.html', 'whales.html', 'whale-flow.html',
                                        'whale-follow.html', 'whale-arch.html',
                                        'whale-tape.html', 'whale-wallets.html']],
    ['portfolio.html',     'Portfolio',['portfolio.html', 'analysis.html', 'live.html']],
  ];

  var here = location.pathname.split('/').pop() || 'index.html';

  var chrome = document.createElement('header');
  chrome.className = 'pc-chrome';

  var section = document.body.getAttribute('data-pc-section') || '';
  var links = '';
  for (var i = 0; i < NAV.length; i++) {
    var on = NAV[i][2].indexOf(here) !== -1;
    if (on && !section) section = NAV[i][1];
    links += '<a href="' + NAV[i][0] + '"' + (on ? ' class="on"' : '') + '>' +
             NAV[i][1] + '</a>';
  }
  if (!section) section = (document.title.split(/[|—–-]/)[0] || 'desk').trim();

  chrome.innerHTML =
    '<a class="pc-brand" href="./">' +
      '<b>POLY<i>//</i>CLAWD</b>' +
      '<span class="pc-sect">' + section.replace(/[<>&]/g, '') + '</span>' +
    '</a>' +
    '<nav class="pc-nav" aria-label="Polyclawd">' + links + '</nav>' +
    '<span class="pc-right"><span class="pc-stamp pc-stamp--accent" id="pc-mode">PAPER</span></span>';

  document.body.insertBefore(chrome, document.body.firstChild);
  document.body.classList.add('pc-shell-on');
})();
