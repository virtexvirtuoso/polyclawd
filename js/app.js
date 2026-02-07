/**
 * Polymarket Trading Bot - Frontend Logic
 * Virtuoso Crypto Styling
 */

// Detect base path from current location (supports /polyclawd/ subdirectory)
const getBasePath = () => {
  const path = window.location.pathname;
  if (path.startsWith('/polyclawd')) {
    return '/polyclawd/api';
  }
  return '/api';
};

const API_BASE = getBasePath();

// ============================================================================
// API Client
// ============================================================================

const api = {
  async get(endpoint) {
    try {
      const res = await fetch(`${API_BASE}${endpoint}`);
      if (!res.ok) throw new Error(`API Error: ${res.status}`);
      return await res.json();
    } catch (error) {
      console.error(`GET ${endpoint} failed:`, error);
      throw error;
    }
  },

  async post(endpoint, data = {}) {
    try {
      const res = await fetch(`${API_BASE}${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      });
      if (!res.ok) throw new Error(`API Error: ${res.status}`);
      return await res.json();
    } catch (error) {
      console.error(`POST ${endpoint} failed:`, error);
      throw error;
    }
  }
};

// ============================================================================
// Utilities
// ============================================================================

function formatCurrency(value, decimals = 2) {
  const num = parseFloat(value) || 0;
  return '$' + num.toLocaleString('en-US', { 
    minimumFractionDigits: decimals, 
    maximumFractionDigits: decimals 
  });
}

function formatPrice(value) {
  return '$' + parseFloat(value).toFixed(4);
}

function formatPercent(value) {
  const num = parseFloat(value) || 0;
  const sign = num >= 0 ? '+' : '';
  return sign + num.toFixed(2) + '%';
}

function formatPnL(value) {
  const num = parseFloat(value) || 0;
  const sign = num >= 0 ? '+' : '';
  return sign + formatCurrency(num);
}

function truncateText(text, maxLength = 50) {
  if (!text) return '';
  return text.length > maxLength ? text.slice(0, maxLength) + '...' : text;
}

function getPnLClass(value) {
  const num = parseFloat(value) || 0;
  return num >= 0 ? 'positive' : 'negative';
}

function timeAgo(dateString) {
  const date = new Date(dateString);
  const now = new Date();
  const seconds = Math.floor((now - date) / 1000);
  
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
  if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
  return Math.floor(seconds / 86400) + 'd ago';
}

// ============================================================================
// Toast Notifications
// ============================================================================

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container') || createToastContainer();
  
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <span class="toast-icon">${type === 'success' ? '✓' : type === 'error' ? '✕' : 'ℹ'}</span>
    <span>${message}</span>
  `;
  
  container.appendChild(toast);
  
  setTimeout(() => {
    toast.style.opacity = '0';
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

function createToastContainer() {
  const container = document.createElement('div');
  container.id = 'toast-container';
  container.className = 'toast-container';
  document.body.appendChild(container);
  return container;
}

// ============================================================================
// Loading State
// ============================================================================

function showLoading(elementId) {
  const el = document.getElementById(elementId);
  if (el) {
    el.innerHTML = `
      <div class="loading">
        <div class="spinner"></div>
        <span>Loading...</span>
      </div>
    `;
  }
}

function showEmpty(elementId, message = 'No data available') {
  const el = document.getElementById(elementId);
  if (el) {
    el.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">📊</div>
        <p>${message}</p>
      </div>
    `;
  }
}

function showError(elementId, message = 'Failed to load data') {
  const el = document.getElementById(elementId);
  if (el) {
    el.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">⚠️</div>
        <p class="text-error">${message}</p>
      </div>
    `;
  }
}

// ============================================================================
// Dashboard Functions
// ============================================================================

async function loadDashboard() {
  try {
    showLoading('stats-grid');
    showLoading('positions-table');
    showLoading('recent-trades');
    
    // Load Polymarket paper trading data (default)
    const data = await api.get('/paper/polymarket/status');
    
    renderStats(data);
    renderPositions(data.positions || []);
    renderRecentTrades(data.trades || []);
    
  } catch (error) {
    console.error('Dashboard load failed:', error);
    showError('stats-grid', 'Failed to load dashboard data');
  }
}

function renderStats(data) {
  const statsGrid = document.getElementById('stats-grid');
  if (!statsGrid) return;
  
  const totalValue = data.balance + data.total_invested;
  const pnl = data.total_pnl || 0;
  const pnlPct = data.resolved_count > 0 ? (pnl / (data.total_invested || 10000)) * 100 : 0;
  const winRate = data.resolved_count > 0 ? ((data.wins / data.resolved_count) * 100).toFixed(0) : '-';
  
  statsGrid.innerHTML = `
    <div class="card">
      <div class="card-header">💰 Cash Balance</div>
      <div class="card-value">${formatCurrency(data.balance)}</div>
    </div>
    <div class="card">
      <div class="card-header">📈 Invested</div>
      <div class="card-value">${formatCurrency(data.total_invested)}</div>
    </div>
    <div class="card">
      <div class="card-header">🎯 Positions</div>
      <div class="card-value">${data.open_positions} open</div>
    </div>
    <div class="card">
      <div class="card-header">📊 Record</div>
      <div class="card-value">${data.wins}W-${data.losses}L (${winRate}%)</div>
    </div>
  `;
}

function renderPositions(positions) {
  const tableContainer = document.getElementById('positions-table');
  const cardsContainer = document.getElementById('positions-cards');
  
  if (!positions || positions.length === 0) {
    if (tableContainer) showEmpty('positions-table', 'No open positions');
    if (cardsContainer) cardsContainer.innerHTML = '<div class="empty-state"><p>No open positions</p></div>';
    return;
  }
  
  // Filter to only show open positions
  const openPositions = positions.filter(p => p.status === 'open');
  
  if (openPositions.length === 0) {
    if (tableContainer) showEmpty('positions-table', 'No open positions');
    if (cardsContainer) cardsContainer.innerHTML = '<div class="empty-state"><p>No open positions</p></div>';
    return;
  }
  
  // Render table (desktop)
  if (tableContainer) {
    let tableHtml = `
      <table>
        <thead>
          <tr>
            <th>Market</th>
            <th>Side</th>
            <th>Shares</th>
            <th>Entry</th>
            <th>Cost</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
    `;
    
    for (const pos of openPositions) {
      tableHtml += `
        <tr>
          <td class="cell-mono">${truncateText(pos.market, 40)}</td>
          <td><span class="badge ${pos.side === 'YES' ? 'badge-success' : 'badge-error'}">${pos.side}</span></td>
          <td class="cell-mono">${pos.shares.toFixed(0)}</td>
          <td class="cell-mono">${formatPrice(pos.entry_price)}</td>
          <td class="cell-mono">${formatCurrency(pos.cost_basis)}</td>
          <td class="cell-mono text-secondary">${pos.source || 'manual'}</td>
        </tr>
      `;
    }
    
    tableHtml += '</tbody></table>';
    tableContainer.innerHTML = tableHtml;
  }
  
  // Render cards (mobile)
  if (cardsContainer) {
    let cardsHtml = '';
    
    for (const pos of openPositions) {
      cardsHtml += `
        <div class="position-card">
          <div class="position-card-header">
            <div class="position-card-market">${pos.market}</div>
            <span class="badge ${pos.side === 'YES' ? 'badge-success' : 'badge-error'}">${pos.side}</span>
          </div>
          <div class="position-card-stats">
            <div class="position-card-stat">
              <div class="position-card-stat-label">Shares</div>
              <div class="position-card-stat-value">${pos.shares.toFixed(0)}</div>
            </div>
            <div class="position-card-stat">
              <div class="position-card-stat-label">Entry</div>
              <div class="position-card-stat-value">${formatPrice(pos.entry_price)}</div>
            </div>
            <div class="position-card-stat">
              <div class="position-card-stat-label">Cost</div>
              <div class="position-card-stat-value">${formatCurrency(pos.cost_basis)}</div>
            </div>
          </div>
        </div>
      `;
    }
    
    cardsContainer.innerHTML = cardsHtml;
  }
}

function renderRecentTrades(trades) {
  const container = document.getElementById('recent-trades');
  if (!container) return;
  
  if (!trades || trades.length === 0) {
    container.innerHTML = '<p class="text-secondary">No recent trades</p>';
    return;
  }
  
  let html = '<div class="section-header">Recent Trades</div>';
  
  // Sort by timestamp descending (most recent first)
  const sortedTrades = [...trades].sort((a, b) => 
    new Date(b.timestamp || b.opened_at) - new Date(a.timestamp || a.opened_at)
  );
  
  for (const trade of sortedTrades.slice(0, 5)) {
    const sideClass = trade.side === 'YES' ? 'badge-success' : 'badge-error';
    const statusClass = trade.status === 'resolved' ? 'badge-secondary' : 'badge-success';
    const market = trade.market || trade.market_question || 'Unknown';
    const cost = trade.cost_basis || trade.amount || 0;
    const tradeTime = trade.opened_at || trade.timestamp;
    
    html += `
      <div class="flex items-center justify-between mb-1" style="padding: 0.5rem 0; border-bottom: 1px solid var(--border);">
        <div>
          <span class="badge ${sideClass}">${trade.side}</span>
          <span class="text-mono ml-1">${trade.shares?.toFixed(0) || '?'} shares</span>
          <span class="text-secondary ml-1">${truncateText(market, 30)}</span>
        </div>
        <div class="text-right">
          <div class="text-mono">${formatCurrency(cost)}</div>
          <div class="text-secondary" style="font-size: 0.75rem;">${tradeTime ? timeAgo(tradeTime) : ''}</div>
        </div>
      </div>
    `;
  }
  
  container.innerHTML = html;
}

// ============================================================================
// Arb Scanner Functions
// ============================================================================

let arbRefreshInterval = null;

async function loadArbScanner() {
  await scanForArb();
}

async function scanForArb() {
  try {
    showLoading('arb-results');
    document.getElementById('scan-btn')?.setAttribute('disabled', 'true');
    
    const data = await api.get('/arb-scan');
    renderArbResults(data);
    updateLastScan();
    
  } catch (error) {
    console.error('Arb scan failed:', error);
    showError('arb-results', 'Failed to scan for arbitrage opportunities');
  } finally {
    document.getElementById('scan-btn')?.removeAttribute('disabled');
  }
}

function renderArbResults(data) {
  const container = document.getElementById('arb-results');
  if (!container) return;
  
  const opportunities = data.opportunities || [];
  
  if (opportunities.length === 0) {
    showEmpty('arb-results', 'No arbitrage opportunities found. Yes + No prices are close to $1.00 in active markets.');
    return;
  }
  
  let html = `
    <table>
      <thead>
        <tr>
          <th>Market</th>
          <th>Yes Price</th>
          <th>No Price</th>
          <th>Sum</th>
          <th>Spread</th>
          <th>Type</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
  `;
  
  for (const opp of opportunities) {
    const typeClass = opp.type === 'underpriced' ? 'badge-success' : 'badge-error';
    const typeBadge = opp.type === 'underpriced' ? 'BUY BOTH' : 'OVERPRICED';
    
    html += `
      <tr>
        <td>${truncateText(opp.question, 45)}</td>
        <td class="cell-mono">${formatPrice(opp.yes_price)}</td>
        <td class="cell-mono">${formatPrice(opp.no_price)}</td>
        <td class="cell-mono">${formatPrice(opp.total)}</td>
        <td class="cell-mono text-primary">${(opp.spread * 100).toFixed(2)}%</td>
        <td><span class="badge ${typeClass}">${typeBadge}</span></td>
        <td>
          <button class="btn btn-sm btn-secondary" onclick="showArbDetails('${opp.market_id}')">
            Details
          </button>
        </td>
      </tr>
    `;
  }
  
  html += '</tbody></table>';
  container.innerHTML = html;
}

function updateLastScan() {
  const el = document.getElementById('last-scan');
  if (el) {
    el.textContent = new Date().toLocaleTimeString();
  }
}

function toggleAutoRefresh() {
  const btn = document.getElementById('auto-refresh-btn');
  
  if (arbRefreshInterval) {
    clearInterval(arbRefreshInterval);
    arbRefreshInterval = null;
    btn.textContent = 'Auto-Refresh: OFF';
    btn.classList.remove('btn-success');
    btn.classList.add('btn-secondary');
  } else {
    arbRefreshInterval = setInterval(scanForArb, 30000);
    btn.textContent = 'Auto-Refresh: ON';
    btn.classList.remove('btn-secondary');
    btn.classList.add('btn-success');
  }
}

// ============================================================================
// Liquidity Rewards Functions
// ============================================================================

async function loadRewards() {
  try {
    showLoading('rewards-table');
    
    const data = await api.get('/rewards');
    renderRewards(data);
    
  } catch (error) {
    console.error('Rewards load failed:', error);
    showError('rewards-table', 'Failed to load liquidity reward opportunities');
  }
}

function renderRewards(data) {
  const container = document.getElementById('rewards-table');
  if (!container) return;
  
  const opportunities = data.opportunities || [];
  
  if (opportunities.length === 0) {
    showEmpty('rewards-table', 'No liquidity reward opportunities found');
    return;
  }
  
  let html = `
    <table>
      <thead>
        <tr>
          <th>Rank</th>
          <th>Market</th>
          <th>Score</th>
          <th>Daily Pool</th>
          <th>Min Size</th>
          <th>Max Spread</th>
          <th>Competition</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
  `;
  
  opportunities.forEach((opp, index) => {
    const compClass = opp.competitive < 0.3 ? 'cell-positive' : opp.competitive > 0.7 ? 'cell-negative' : '';
    
    html += `
      <tr>
        <td class="cell-mono">#${index + 1}</td>
        <td>${truncateText(opp.question, 40)}</td>
        <td class="cell-mono text-primary">${opp.opportunity_score.toFixed(1)}</td>
        <td class="cell-mono">${formatCurrency(opp.daily_reward_rate)}</td>
        <td class="cell-mono">${opp.rewards_min_size.toFixed(0)}</td>
        <td class="cell-mono">${opp.rewards_max_spread.toFixed(1)}¢</td>
        <td class="cell-mono ${compClass}">${(opp.competitive * 100).toFixed(1)}%</td>
        <td>
          <button class="btn btn-sm btn-secondary" onclick="showRewardDetails('${opp.market_id}')">
            Analyze
          </button>
        </td>
      </tr>
    `;
  });
  
  html += '</tbody></table>';
  container.innerHTML = html;
}

async function showRewardDetails(marketId) {
  // Simple alert for now, could expand to modal
  showToast(`Analyzing market ${marketId}...`, 'info');
}

// ============================================================================
// Paper Trading Functions
// ============================================================================

async function loadTradePage() {
  try {
    showLoading('positions-list');
    showLoading('trades-list');
    
    const [positions, trades, balance] = await Promise.all([
      api.get('/positions'),
      api.get('/trades?limit=20'),
      api.get('/balance')
    ]);
    
    renderTradeBalance(balance);
    renderTradePositions(positions);
    renderTradeHistory(trades);
    
  } catch (error) {
    console.error('Trade page load failed:', error);
  }
}

function renderTradeBalance(balance) {
  const el = document.getElementById('trade-balance');
  if (el) {
    el.innerHTML = `Cash: <span class="text-primary">${formatCurrency(balance.cash)}</span>`;
  }
}

function renderTradePositions(positions) {
  const container = document.getElementById('positions-list');
  if (!container) return;
  
  if (!positions || positions.length === 0) {
    showEmpty('positions-list', 'No open positions');
    return;
  }
  
  let html = '';
  for (const pos of positions) {
    const pnlClass = getPnLClass(pos.pnl);
    html += `
      <div class="card mb-1">
        <div class="flex justify-between items-center">
          <div>
            <div class="text-mono mb-1">${truncateText(pos.market_question, 35)}</div>
            <div class="text-secondary text-mono" style="font-size: 0.75rem;">
              ${pos.shares.toFixed(2)} shares @ ${formatPrice(pos.entry_price)}
            </div>
          </div>
          <div class="text-right">
            <div class="text-mono ${pnlClass}">${formatPnL(pos.pnl)}</div>
            <button class="btn btn-sm btn-danger mt-1" onclick="sellPosition('${pos.market_id}', '${pos.side}', ${pos.current_value})">
              Sell All
            </button>
          </div>
        </div>
      </div>
    `;
  }
  
  container.innerHTML = html;
}

function renderTradeHistory(trades) {
  const container = document.getElementById('trades-list');
  if (!container) return;
  
  const validTrades = trades.filter(t => t.type !== 'RESET');
  
  if (validTrades.length === 0) {
    showEmpty('trades-list', 'No trade history');
    return;
  }
  
  let html = '';
  for (const trade of validTrades.slice(0, 15)) {
    const badgeClass = trade.type === 'BUY' ? 'badge-success' : 'badge-error';
    html += `
      <div style="padding: 0.75rem 0; border-bottom: 1px solid var(--border);">
        <div class="flex justify-between items-center">
          <div>
            <span class="badge ${badgeClass}">${trade.type}</span>
            <span class="text-mono" style="margin-left: 0.5rem;">${trade.side}</span>
          </div>
          <div class="text-mono text-secondary" style="font-size: 0.75rem;">
            ${timeAgo(trade.timestamp)}
          </div>
        </div>
        <div class="text-secondary mt-1" style="font-size: 0.85rem;">
          ${truncateText(trade.market_question, 40)}
        </div>
        <div class="text-mono mt-1">
          ${trade.shares.toFixed(2)} @ ${formatPrice(trade.price)} = ${formatCurrency(trade.amount)}
          ${trade.pnl !== undefined ? `<span class="${getPnLClass(trade.pnl)}">(${formatPnL(trade.pnl)})</span>` : ''}
        </div>
      </div>
    `;
  }
  
  container.innerHTML = html;
}

async function searchMarkets() {
  const query = document.getElementById('market-search')?.value;
  if (!query || query.length < 2) {
    showToast('Enter at least 2 characters to search', 'error');
    return;
  }
  
  try {
    showLoading('search-results');
    const data = await api.get(`/markets/search?q=${encodeURIComponent(query)}`);
    renderSearchResults(data.markets || []);
  } catch (error) {
    showError('search-results', 'Search failed');
  }
}

function renderSearchResults(markets) {
  const container = document.getElementById('search-results');
  if (!container) return;
  
  if (markets.length === 0) {
    showEmpty('search-results', 'No markets found');
    return;
  }
  
  let html = '';
  for (const market of markets) {
    html += `
      <div class="card mb-1" style="cursor: pointer;" onclick="selectMarket('${market.id}', '${market.question.replace(/'/g, "\\'")}')">
        <div class="text-mono mb-1">${truncateText(market.question, 50)}</div>
        <div class="flex gap-2 text-secondary" style="font-size: 0.75rem;">
          <span>Yes: ${formatPrice(market.yes_price)}</span>
          <span>No: ${formatPrice(market.no_price)}</span>
          <span>Vol: ${formatCurrency(market.volume_24h, 0)}</span>
        </div>
      </div>
    `;
  }
  
  container.innerHTML = html;
}

function selectMarket(id, question) {
  document.getElementById('market-id').value = id;
  showToast(`Selected: ${truncateText(question, 40)}`, 'success');
}

async function executeTrade() {
  const marketId = document.getElementById('market-id')?.value;
  const side = document.getElementById('trade-side')?.value;
  const amount = parseFloat(document.getElementById('trade-amount')?.value);
  
  if (!marketId || !side || !amount || amount <= 0) {
    showToast('Please fill in all fields correctly', 'error');
    return;
  }
  
  try {
    const btn = document.getElementById('trade-btn');
    btn.disabled = true;
    btn.textContent = 'Processing...';
    
    const result = await api.post('/trade', {
      market_id: marketId,
      side: side,
      amount: amount,
      type: 'BUY'
    });
    
    showToast(`Bought ${result.shares.toFixed(2)} ${side} shares!`, 'success');
    
    // Clear form and reload
    document.getElementById('trade-amount').value = '';
    loadTradePage();
    
  } catch (error) {
    showToast('Trade failed: ' + error.message, 'error');
  } finally {
    const btn = document.getElementById('trade-btn');
    btn.disabled = false;
    btn.textContent = 'Buy';
  }
}

async function sellPosition(marketId, side, amount) {
  if (!confirm(`Sell entire ${side} position for ~${formatCurrency(amount)}?`)) {
    return;
  }
  
  try {
    const result = await api.post('/trade', {
      market_id: marketId,
      side: side,
      amount: amount,
      type: 'SELL'
    });
    
    const pnl = result.pnl || 0;
    showToast(`Sold position! P&L: ${formatPnL(pnl)}`, pnl >= 0 ? 'success' : 'error');
    
    // Reload appropriate page
    if (typeof loadDashboard === 'function' && document.getElementById('stats-grid')) {
      loadDashboard();
    } else if (typeof loadTradePage === 'function' && document.getElementById('positions-list')) {
      loadTradePage();
    }
    
  } catch (error) {
    showToast('Sell failed: ' + error.message, 'error');
  }
}

async function resetPaperTrading() {
  if (!confirm('Reset paper trading? This will clear all positions and reset balance to $10,000.')) {
    return;
  }
  
  try {
    await api.post('/reset');
    showToast('Paper trading reset to $10,000', 'success');
    loadTradePage();
  } catch (error) {
    showToast('Reset failed', 'error');
  }
}

// ============================================================================
// Markets Browser Functions
// ============================================================================

async function loadMarkets() {
  try {
    showLoading('markets-grid');
    
    const data = await api.get('/markets/trending');
    renderMarketsGrid(data.markets || []);
    
  } catch (error) {
    console.error('Markets load failed:', error);
    showError('markets-grid', 'Failed to load trending markets');
  }
}

async function searchAllMarkets() {
  const query = document.getElementById('markets-search')?.value;
  if (!query || query.length < 2) {
    loadMarkets(); // Reload trending
    return;
  }
  
  try {
    showLoading('markets-grid');
    const data = await api.get(`/markets/search?q=${encodeURIComponent(query)}`);
    renderMarketsGrid(data.markets || []);
  } catch (error) {
    showError('markets-grid', 'Search failed');
  }
}

function renderMarketsGrid(markets) {
  const container = document.getElementById('markets-grid');
  if (!container) return;
  
  if (markets.length === 0) {
    showEmpty('markets-grid', 'No markets found');
    return;
  }
  
  let html = '';
  for (const market of markets) {
    const yesPercent = (parseFloat(market.yes_price) * 100).toFixed(0);
    
    html += `
      <div class="card" onclick="showMarketModal('${market.id}')">
        <div class="text-mono mb-2" style="font-size: 0.9rem;">${truncateText(market.question, 60)}</div>
        
        <div class="progress mb-2">
          <div class="progress-bar" style="width: ${yesPercent}%"></div>
        </div>
        
        <div class="flex justify-between text-mono" style="font-size: 0.8rem;">
          <span class="text-success">Yes ${formatPrice(market.yes_price)}</span>
          <span class="text-error">No ${formatPrice(market.no_price)}</span>
        </div>
        
        <div class="text-secondary mt-2" style="font-size: 0.75rem;">
          24h Vol: ${formatCurrency(market.volume_24h, 0)}
        </div>
      </div>
    `;
  }
  
  container.innerHTML = `<div class="grid-3">${html}</div>`;
}

async function showMarketModal(marketId) {
  // TODO: Implement modal with market details
  showToast(`Market ID: ${marketId}`, 'info');
}

// ============================================================================
// Initialization
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
  // Determine which page we're on and initialize
  const path = window.location.pathname;
  
  if (path.includes('arb.html')) {
    loadArbScanner();
  } else if (path.includes('rewards.html')) {
    loadRewards();
  } else if (path.includes('trade.html')) {
    loadTradePage();
  } else if (path.includes('markets.html')) {
    loadMarkets();
  } else {
    // Default: dashboard
    loadDashboard();
  }
  
  // Setup search listeners
  document.getElementById('market-search')?.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') searchMarkets();
  });
  
  document.getElementById('markets-search')?.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') searchAllMarkets();
  });
});
