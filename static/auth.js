// Polyclawd Access Gate
(function() {
  const ACCESS_CODE_HASH = '9ac3acd6f058770ae5779e25a7ecb05bfb85553545ebdbbb06f4d0352dc6d3bb'; // SHA-256 of access code (rotated 2026-05-15)
  const AUTH_KEY = 'polyclawd_auth';
  const currentPage = window.location.pathname.split('/').pop() || 'index.html';

  async function sha256(message) {
    const msgBuffer = new TextEncoder().encode(message);
    const hashBuffer = await crypto.subtle.digest('SHA-256', msgBuffer);
    const hashArray = Array.from(new Uint8Array(hashBuffer));
    return hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
  }

  function isAuthed() {
    return localStorage.getItem(AUTH_KEY) === 'true';
  }

  function showGate() {
    // Don't gate the login page itself
    if (currentPage === 'login.html') return;

    if (!isAuthed()) {
      window.location.href = 'login.html?r=' + encodeURIComponent(currentPage);
    }
  }

  async function handleAccessCode(code) {
    const hash = await sha256(code);
    if (hash === ACCESS_CODE_HASH) {
      localStorage.setItem(AUTH_KEY, 'true');
      // Track visitor
      trackVisitor();
      // Redirect
      const params = new URLSearchParams(window.location.search);
      const redirect = params.get('r') || 'portfolio.html';
      window.location.href = redirect;
      return true;
    }
    return false;
  }

  function trackVisitor() {
    try {
      const payload = {
        timestamp: new Date().toISOString(),
        page: currentPage,
        userAgent: navigator.userAgent,
        screenSize: screen.width + 'x' + screen.height,
        language: navigator.language,
        referrer: document.referrer || 'direct'
      };
      // Fire and forget — don't block login
      fetch('/polyclawd/api/visitor-log', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).catch(() => {});
    } catch(e) {}
  }

  function handleLogout() {
    localStorage.removeItem(AUTH_KEY);
    window.location.href = 'login.html';
  }

  // Expose globally
  window.polyclawdAuth = {
    isAuthed,
    showGate,
    handleAccessCode,
    handleLogout
  };

  // Auto-gate on load (except login page)
  if (currentPage !== 'login.html') {
    showGate();
  }
})();
