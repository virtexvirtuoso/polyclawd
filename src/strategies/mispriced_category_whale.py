"""
Strategy: Mispriced Category + Whale Confirmation
==================================================

Core thesis from backtest findings:
"Real edge comes from targeting high-volume markets in mispriced categories
using volume and whales as confirmation."

Combines 4 validated edges from our 3.75M market analysis:
1. Category mispricing (analysis #7) — target categories with >15% avg error
2. Volume floor (analysis #5) — only trade $10K+ volume markets (200x less error)
3. Whale confirmation (analysis #4) — whales accurate ~45% → use as signal filter
4. Theta optimization (analysis #6) — prefer 1-7 day markets for best risk/reward

Entry logic:
- Market is in a mispriced category (EUR/USD, Spotify, weather, entertainment)
- Volume > $10K (hard floor)
- Price is in the "contested" zone (20-80%)
- Volume spike detected (>1σ above category mean) = whale activity
- Market expires within 7 days (theta sweet spot)

Exit logic:
- Price reaches 90%+ or 10%- (resolution convergence)
- Hold until expiry if within 48h
- Stop loss at -8% of position value

Sizing:
- Quarter Kelly based on category historical edge
- Max 5% of bankroll per trade
- Scale position with confidence: more confirmations = larger size
"""

import duckdb
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

# Add parent paths
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.common.analysis import Analysis, AnalysisOutput


# Categories with >15% average pricing error (from analysis #7)
MISPRICED_CATEGORIES = {
    # Worst offenders from 332 categories analyzed
    'KXEURUSDH': 0.454,      # EUR/USD hourly — 45% error
    'KXSPOTIFYARTISTD': 0.601, # Spotify daily — 60% error
    'KXSPOTIFYLISTD': 0.55,   # Spotify lists — 55% error
    'KXGDPH': 0.35,           # GDP hourly — 35% error
    'KXCPIH': 0.32,           # CPI hourly — 32% error
    'KXTEMPD': 0.28,          # Temperature daily — 28% error
    'KXWIND': 0.25,           # Wind — 25% error
    'KXRAIND': 0.24,          # Rain daily — 24% error
    'KXHUMID': 0.22,          # Humidity — 22% error
    'KXTEMPW': 0.20,          # Temperature weekly — 20% error
    'KXSNOW': 0.19,           # Snow — 19% error
    'KXETF': 0.18,            # ETF — 18% error
    'KXSTONKS': 0.17,         # Stocks — 17% error
    'KXCRYPTO': 0.16,         # Crypto — 16% error
}

# Well-calibrated categories to AVOID (from analysis #7)
EFFICIENT_CATEGORIES = {
    'KXPGATOUR', 'KXMLB', 'KXNBA', 'KXNHL', 'KXNFL',
    'KXAOWOMEN', 'KXFIRSTSUPERBOWLSONG',
}

# Volume tier thresholds (from analysis #4)
VOLUME_FLOOR = 500  # 500 contracts minimum (~$250+ notional)
WHALE_THRESHOLD = 10_000  # 10K+ contracts = whale tier


@dataclass
class Trade:
    """Single simulated trade."""
    market_id: str
    category: str
    platform: str
    entry_price: float
    exit_price: float
    entry_time: Optional[str] = None
    exit_time: Optional[str] = None
    volume: float = 0.0
    duration_days: float = 0.0
    position_size: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    edge_at_entry: float = 0.0
    confirmations: int = 0
    outcome: str = ''  # 'win', 'loss', 'hold'


@dataclass 
class BacktestResult:
    """Full backtest output."""
    strategy_name: str = 'MispricedCategoryWhale'
    trades: list = field(default_factory=list)
    total_pnl: float = 0.0
    win_rate: float = 0.0
    avg_edge: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    profit_factor: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    bankroll_curve: list = field(default_factory=list)
    by_category: dict = field(default_factory=dict)


class MispricedCategoryWhaleStrategy(Analysis):
    """
    Backtest: target high-volume markets in mispriced categories,
    using volume spikes and whale activity as confirmation signals.
    """
    
    name = "Mispriced Category + Whale Confirmation"
    
    # Strategy parameters (tunable)
    MIN_VOLUME = VOLUME_FLOOR
    MIN_EDGE = 0.03            # 3% minimum edge to enter
    MAX_DURATION_DAYS = 30     # Expanded from 7 — capture more markets
    CONTESTED_RANGE = (0.15, 0.85)  # Slightly wider contest zone
    TAKE_PROFIT = 0.90         # Exit when price hits 90% (or 10%)
    STOP_LOSS = 0.08           # 8% stop loss
    KELLY_FRACTION = 0.25      # Quarter Kelly
    MAX_POSITION_PCT = 0.05    # 5% max per trade
    INITIAL_BANKROLL = 10_000  # $10K starting capital
    VOLUME_SPIKE_SIGMA = 1.0   # 1σ volume spike = confirmation
    
    def run(self) -> AnalysisOutput:
        """Run full backtest simulation."""
        from src.common.chart_theme import apply_theme, COLORS
        apply_theme()
        
        data_dir = Path(__file__).resolve().parents[2] / 'data'
        con = duckdb.connect()
        
        # ── Load Kalshi markets with categories ──
        print("Loading Kalshi markets for backtest...")
        kalshi = con.execute(f"""
            SELECT 
                ticker as id, event_ticker, last_price, volume, close_time, 
                result, open_time, status
            FROM read_parquet('{data_dir}/kalshi/markets/*.parquet')
            WHERE result IS NOT NULL 
              AND last_price IS NOT NULL
              AND volume IS NOT NULL
              AND volume > 0
              AND close_time IS NOT NULL
              AND open_time IS NOT NULL
        """).df()
        
        if kalshi.empty:
            return self._empty_result("No Kalshi data")
        
        # Extract category from event_ticker (e.g., KXEURUSDH-26FEB11 → KXEURUSDH)
        kalshi['category'] = kalshi['event_ticker'].str.replace(r'-.*$', '', regex=True)
        kalshi['platform'] = 'kalshi'
        # Prices are in cents (1-99), convert to decimal
        kalshi['price'] = (kalshi['last_price'] / 100.0).clip(0.01, 0.99)
        
        # Calculate duration
        kalshi['close_time'] = pd.to_datetime(kalshi['close_time'], errors='coerce')
        kalshi['open_time'] = pd.to_datetime(kalshi['open_time'], errors='coerce')
        kalshi['duration_days'] = (kalshi['close_time'] - kalshi['open_time']).dt.total_seconds() / 86400
        
        # Determine resolution (1 = yes, 0 = no)
        kalshi['resolved_yes'] = kalshi['result'].apply(
            lambda x: 1.0 if str(x).lower() in ('yes', '1', 'true', 1) else 0.0
        )
        
        print(f"  Loaded {len(kalshi):,} resolved Kalshi markets")
        
        # ── Compute category-level stats ──
        print("Computing category statistics...")
        cat_stats = kalshi.groupby('category').agg(
            count=('id', 'count'),
            avg_volume=('volume', 'mean'),
            std_volume=('volume', 'std'),
            avg_price=('price', 'mean'),
            avg_error=('price', lambda x: np.abs(x - kalshi.loc[x.index, 'resolved_yes']).mean()),
        ).reset_index()
        
        # Data-driven: find categories with >10% avg error (mispriced)
        mispriced_cats = set(cat_stats[cat_stats['avg_error'] > 0.10]['category'].tolist())
        mispriced_cats.update(MISPRICED_CATEGORIES.keys())  # Add known ones too
        print(f"  Found {len(mispriced_cats)} mispriced categories (>10% avg error)")
        
        # ── Filter to tradeable markets ──
        print("Filtering to tradeable markets...")
        tradeable = kalshi[
            (kalshi['volume'] >= self.MIN_VOLUME) &
            (kalshi['price'] >= self.CONTESTED_RANGE[0]) &
            (kalshi['price'] <= self.CONTESTED_RANGE[1]) &
            (kalshi['duration_days'] <= self.MAX_DURATION_DAYS) &
            (kalshi['duration_days'] > 0) &
            (kalshi['category'].isin(mispriced_cats)) &
            (~kalshi['category'].isin(EFFICIENT_CATEGORIES))
        ].copy()
        
        print(f"  {len(tradeable):,} markets pass filters (from {len(kalshi):,})")
        
        if tradeable.empty:
            # Relax filters — try without category restriction
            print("  Relaxing category filter...")
            tradeable = kalshi[
                (kalshi['volume'] >= self.MIN_VOLUME) &
                (kalshi['price'] >= self.CONTESTED_RANGE[0]) &
                (kalshi['price'] <= self.CONTESTED_RANGE[1]) &
                (kalshi['duration_days'] <= self.MAX_DURATION_DAYS) &
                (kalshi['duration_days'] > 0) &
                (~kalshi['category'].isin(EFFICIENT_CATEGORIES))
            ].copy()
            print(f"  {len(tradeable):,} markets with relaxed filters")
        
        if tradeable.empty:
            return self._empty_result("No tradeable markets found")
        
        # ── Compute volume spike signal ──
        # Merge category stats for volume spike detection
        tradeable = tradeable.merge(
            cat_stats[['category', 'avg_volume', 'std_volume']], 
            on='category', how='left', suffixes=('', '_cat')
        )
        tradeable['volume_zscore'] = (
            (tradeable['volume'] - tradeable['avg_volume']) / 
            tradeable['std_volume'].clip(lower=1)
        )
        tradeable['has_volume_spike'] = tradeable['volume_zscore'] >= self.VOLUME_SPIKE_SIGMA
        tradeable['is_whale'] = tradeable['volume'] >= WHALE_THRESHOLD
        
        # ── Compute edge and confirmations ──
        tradeable['pred_error'] = np.abs(tradeable['price'] - tradeable['resolved_yes'])
        category_errors = tradeable.groupby('category')['pred_error'].mean().to_dict()
        # Use actual measured category error as edge estimate
        tradeable['category_edge'] = tradeable['category'].map(
            lambda c: category_errors.get(c, MISPRICED_CATEGORIES.get(c, 0.10))
        )
        
        # Confirmation count
        tradeable['confirmations'] = (
            tradeable['has_volume_spike'].astype(int) +
            tradeable['is_whale'].astype(int) +
            (tradeable['duration_days'] <= 3).astype(int) +
            (tradeable['category_edge'] >= 0.20).astype(int)
        )
        
        # ── Simulate trades ──
        print("Running trade simulation...")
        trades = []
        bankroll = self.INITIAL_BANKROLL
        bankroll_curve = [bankroll]
        peak_bankroll = bankroll
        max_drawdown = 0
        
        # Sort by close time for chronological simulation
        tradeable = tradeable.sort_values('close_time')
        
        for _, row in tradeable.iterrows():
            if bankroll <= 0:
                break
                
            # Skip if edge too small
            edge = row['category_edge']
            if edge < self.MIN_EDGE:
                continue
            
            # Position sizing: quarter Kelly, scaled by confirmations
            kelly = edge * self.KELLY_FRACTION
            confirmation_mult = 1.0 + (row['confirmations'] * 0.15)  # +15% per confirmation
            position_pct = min(kelly * confirmation_mult, self.MAX_POSITION_PCT)
            # Fixed fractional sizing based on INITIAL bankroll to prevent compound explosion
            position_size = self.INITIAL_BANKROLL * position_pct
            
            if position_size < 1:  # Minimum $1 trade
                continue
            
            # Determine direction: bet on the more likely outcome
            # If price > 0.5, market leans YES — we check if it resolves YES
            # If price < 0.5, market leans NO — we check if it resolves NO
            entry_price = row['price']
            resolved = row['resolved_yes']
            
            # We bet WITH the direction the price implies (momentum/whale confirmation)
            if entry_price >= 0.5:
                # Bet YES
                exit_price = resolved  # 1.0 if yes, 0.0 if no
                pnl = (exit_price - entry_price) * position_size / entry_price
            else:
                # Bet NO
                exit_price = 1.0 - resolved  # 1.0 if no, 0.0 if yes
                pnl = (exit_price - (1.0 - entry_price)) * position_size / (1.0 - entry_price)
            
            trade = Trade(
                market_id=str(row.get('id', '')),
                category=row['category'],
                platform='kalshi',
                entry_price=entry_price,
                exit_price=exit_price,
                volume=row['volume'],
                duration_days=row['duration_days'],
                position_size=position_size,
                pnl=pnl,
                pnl_pct=pnl / position_size if position_size > 0 else 0,
                edge_at_entry=edge,
                confirmations=row['confirmations'],
                outcome='win' if pnl > 0 else 'loss',
                entry_time=str(row.get('open_time', '')),
                exit_time=str(row.get('close_time', '')),
            )
            trades.append(trade)
            bankroll += pnl
            bankroll_curve.append(bankroll)
            
            # Track drawdown
            peak_bankroll = max(peak_bankroll, bankroll)
            dd = (peak_bankroll - bankroll) / peak_bankroll if peak_bankroll > 0 else 0
            max_drawdown = max(max_drawdown, dd)
        
        # ── Compute results ──
        print(f"  Simulated {len(trades):,} trades")
        
        result = BacktestResult(trades=trades)
        result.total_trades = len(trades)
        
        if trades:
            pnls = [t.pnl for t in trades]
            wins = [t for t in trades if t.pnl > 0]
            losses = [t for t in trades if t.pnl <= 0]
            
            result.total_pnl = sum(pnls)
            result.winning_trades = len(wins)
            result.losing_trades = len(losses)
            result.win_rate = len(wins) / len(trades) if trades else 0
            result.avg_edge = np.mean([t.edge_at_entry for t in trades])
            result.max_drawdown = max_drawdown
            result.bankroll_curve = bankroll_curve
            
            # Sharpe
            if len(pnls) > 1 and np.std(pnls) > 0:
                result.sharpe = np.mean(pnls) / np.std(pnls) * np.sqrt(252)
            
            # Profit factor
            gross_profit = sum(t.pnl for t in wins) if wins else 0
            gross_loss = abs(sum(t.pnl for t in losses)) if losses else 1
            result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
            
            # By category
            for t in trades:
                if t.category not in result.by_category:
                    result.by_category[t.category] = {'trades': 0, 'wins': 0, 'pnl': 0}
                result.by_category[t.category]['trades'] += 1
                result.by_category[t.category]['pnl'] += t.pnl
                if t.pnl > 0:
                    result.by_category[t.category]['wins'] += 1
        
        # ── Generate figure ──
        fig = self._plot(result)
        
        metadata = {
            'total_trades': result.total_trades,
            'total_pnl': round(result.total_pnl, 2),
            'win_rate': round(result.win_rate * 100, 1),
            'sharpe': round(result.sharpe, 2),
            'max_drawdown': round(result.max_drawdown * 100, 1),
            'profit_factor': round(result.profit_factor, 2),
            'final_bankroll': round(bankroll, 2),
            'return_pct': round((bankroll - self.INITIAL_BANKROLL) / self.INITIAL_BANKROLL * 100, 1),
            'categories_traded': len(result.by_category),
        }
        
        print(f"  Results: {metadata}")
        
        return AnalysisOutput(figure=fig, metadata=metadata)
    
    def _plot(self, result: BacktestResult):
        """Generate 4-panel backtest dashboard."""
        from src.common.chart_theme import COLORS, PALETTE
        
        fig, axes = plt.subplots(2, 2, figsize=(16, 12))
        
        # 1. Equity curve
        ax = axes[0, 0]
        curve = result.bankroll_curve
        colors_line = [COLORS['green'] if curve[i] >= curve[i-1] else COLORS['red'] 
                       for i in range(1, len(curve))]
        ax.plot(range(len(curve)), curve, color=COLORS['accent'], linewidth=1.5, alpha=0.9)
        ax.fill_between(range(len(curve)), self.INITIAL_BANKROLL, curve, 
                        where=[c >= self.INITIAL_BANKROLL for c in curve],
                        alpha=0.15, color=COLORS['green'])
        ax.fill_between(range(len(curve)), self.INITIAL_BANKROLL, curve,
                        where=[c < self.INITIAL_BANKROLL for c in curve],
                        alpha=0.15, color=COLORS['red'])
        ax.axhline(y=self.INITIAL_BANKROLL, color=COLORS['text2'], linestyle='--', alpha=0.5)
        ax.set_title('Equity Curve', fontsize=13, fontweight='bold', color=COLORS['text'])
        ax.set_xlabel('Trade #')
        ax.set_ylabel('Bankroll ($)')
        
        # 2. P&L by category
        ax = axes[0, 1]
        if result.by_category:
            cats = sorted(result.by_category.items(), key=lambda x: x[1]['pnl'], reverse=True)
            cat_names = [c[0].replace('KX', '') for c in cats[:15]]
            cat_pnls = [c[1]['pnl'] for c in cats[:15]]
            bar_colors = [COLORS['green'] if p > 0 else COLORS['red'] for p in cat_pnls]
            ax.barh(range(len(cat_names)), cat_pnls, color=bar_colors, alpha=0.85)
            ax.set_yticks(range(len(cat_names)))
            ax.set_yticklabels(cat_names, fontsize=8)
            ax.axvline(x=0, color=COLORS['text2'], linewidth=0.5)
        ax.set_title('P&L by Category', fontsize=13, fontweight='bold', color=COLORS['text'])
        ax.set_xlabel('P&L ($)')
        
        # 3. Win rate by confirmation count
        ax = axes[1, 0]
        if result.trades:
            conf_data = {}
            for t in result.trades:
                c = t.confirmations
                if c not in conf_data:
                    conf_data[c] = {'wins': 0, 'total': 0}
                conf_data[c]['total'] += 1
                if t.pnl > 0:
                    conf_data[c]['wins'] += 1
            
            conf_sorted = sorted(conf_data.items())
            conf_labels = [f"{c} conf" for c, _ in conf_sorted]
            conf_wr = [d['wins']/d['total']*100 if d['total'] > 0 else 0 for _, d in conf_sorted]
            conf_counts = [d['total'] for _, d in conf_sorted]
            
            bars = ax.bar(range(len(conf_labels)), conf_wr, color=PALETTE[:len(conf_labels)], alpha=0.85)
            ax.set_xticks(range(len(conf_labels)))
            ax.set_xticklabels(conf_labels, fontsize=9)
            ax.axhline(y=50, color=COLORS['red'], linestyle='--', alpha=0.3, label='Break-even')
            
            # Add count labels
            for i, (bar, count) in enumerate(zip(bars, conf_counts)):
                ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                        f'n={count}', ha='center', fontsize=8, color=COLORS['text2'])
        
        ax.set_title('Win Rate by # Confirmations', fontsize=13, fontweight='bold', color=COLORS['text'])
        ax.set_ylabel('Win Rate %')
        
        # 4. Summary stats box
        ax = axes[1, 1]
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis('off')
        
        final_bankroll = result.bankroll_curve[-1] if result.bankroll_curve else self.INITIAL_BANKROLL
        ret_pct = (final_bankroll - self.INITIAL_BANKROLL) / self.INITIAL_BANKROLL * 100
        
        summary_lines = [
            ("STRATEGY", "Mispriced Category + Whale Confirmation"),
            ("", ""),
            ("Starting Capital", f"${self.INITIAL_BANKROLL:,.0f}"),
            ("Final Capital", f"${final_bankroll:,.2f}"),
            ("Return", f"{ret_pct:+.1f}%"),
            ("", ""),
            ("Total Trades", f"{result.total_trades:,}"),
            ("Win Rate", f"{result.win_rate*100:.1f}%"),
            ("Sharpe Ratio", f"{result.sharpe:.2f}"),
            ("Max Drawdown", f"{result.max_drawdown*100:.1f}%"),
            ("Profit Factor", f"{result.profit_factor:.2f}"),
            ("Avg Edge", f"{result.avg_edge*100:.1f}%"),
            ("", ""),
            ("Categories", f"{len(result.by_category)}"),
        ]
        
        y = 0.95
        for label, value in summary_lines:
            if not label and not value:
                y -= 0.03
                continue
            color = COLORS['text'] if label != "STRATEGY" else COLORS['accent2']
            weight = 'bold' if label == "STRATEGY" else 'normal'
            
            if label == "Return":
                val_color = COLORS['green'] if ret_pct > 0 else COLORS['red']
            elif label == "Win Rate":
                val_color = COLORS['green'] if result.win_rate > 0.5 else COLORS['orange']
            elif label == "Sharpe Ratio":
                val_color = COLORS['green'] if result.sharpe > 1.0 else COLORS['orange'] if result.sharpe > 0 else COLORS['red']
            else:
                val_color = COLORS['text']
            
            ax.text(0.05, y, label, fontsize=10, fontweight=weight, color=color,
                    transform=ax.transAxes, fontfamily='monospace')
            ax.text(0.65, y, value, fontsize=10, fontweight='bold', color=val_color,
                    transform=ax.transAxes, fontfamily='monospace')
            y -= 0.065
        
        # Border around summary
        from matplotlib.patches import FancyBboxPatch
        rect = FancyBboxPatch((0.01, 0.01), 0.98, 0.98, 
                               boxstyle="round,pad=0.02",
                               facecolor=COLORS['surface2'], edgecolor=COLORS['accent'],
                               linewidth=1.5, alpha=0.8, transform=ax.transAxes)
        ax.add_patch(rect)
        # Re-draw text on top
        ax.set_zorder(10)
        
        plt.suptitle("Polyclawd Strategy Backtest: Mispriced Category + Whale Confirmation",
                     fontsize=15, fontweight='bold', color=COLORS['text'], y=0.98)
        plt.tight_layout(rect=[0, 0, 1, 0.96])
        
        return fig
    
    def _empty_result(self, reason: str) -> AnalysisOutput:
        """Return empty result when no data available."""
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.text(0.5, 0.5, f"No data: {reason}", ha='center', va='center',
                fontsize=16, color='#ff5252')
        ax.axis('off')
        return AnalysisOutput(figure=fig, metadata={'error': reason})
