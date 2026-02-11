"""Whale wallet profitability proxy using market volume tiers.

Since on-chain trade scanning is too slow (37GB), we approximate wallet behavior
by analyzing market volume distributions and resolution patterns. This gives
Polyclawd insight into which volume tiers are profitable.
"""

from __future__ import annotations
from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.common.analysis import Analysis, AnalysisOutput


class WhaleWalletProfitabilityAnalysis(Analysis):
    """Proxy whale analysis using market volume tiers and resolution patterns."""

    def __init__(self):
        super().__init__(
            name="whale_wallet_profitability",
            description="Market profitability by volume tier (proxy for whale analysis)",
        )
        base_dir = Path(__file__).parent.parent.parent.parent
        self.poly_markets = base_dir / "data" / "polymarket" / "markets"
        self.kalshi_markets = base_dir / "data" / "kalshi" / "markets"

    def run(self) -> AnalysisOutput:
        con = duckdb.connect()

        with self.progress("Loading Kalshi markets by volume tier"):
            kalshi = con.execute(f"""
                SELECT
                    'kalshi' AS platform,
                    volume,
                    last_price / 100.0 AS price,
                    result,
                    CASE WHEN result = 'yes' THEN 1.0 ELSE 0.0 END AS resolved_yes,
                    CASE
                        WHEN volume >= 100000 THEN 'Whale (100K+)'
                        WHEN volume >= 10000 THEN 'Large (10K-100K)'
                        WHEN volume >= 1000 THEN 'Medium (1K-10K)'
                        WHEN volume >= 100 THEN 'Small (100-1K)'
                        ELSE 'Micro (<100)'
                    END AS tier
                FROM '{self.kalshi_markets}/*.parquet'
                WHERE status = 'finalized' AND result IN ('yes', 'no')
                  AND last_price BETWEEN 1 AND 99 AND volume > 0
            """).df()

        with self.progress("Loading Polymarket markets by volume tier"):
            poly = con.execute(f"""
                SELECT
                    'polymarket' AS platform,
                    volume,
                    CAST(json_extract(outcome_prices, '$[0]') AS DOUBLE) AS price,
                    CASE
                        WHEN CAST(json_extract(outcome_prices, '$[0]') AS DOUBLE) > 0.95 THEN 'yes'
                        WHEN CAST(json_extract(outcome_prices, '$[0]') AS DOUBLE) < 0.05 THEN 'no'
                        ELSE NULL END AS result,
                    CASE WHEN CAST(json_extract(outcome_prices, '$[0]') AS DOUBLE) > 0.95 THEN 1.0 ELSE 0.0 END AS resolved_yes,
                    CASE
                        WHEN volume >= 1000000 THEN 'Whale (100K+)'
                        WHEN volume >= 100000 THEN 'Large (10K-100K)'
                        WHEN volume >= 10000 THEN 'Medium (1K-10K)'
                        WHEN volume >= 1000 THEN 'Small (100-1K)'
                        ELSE 'Micro (<100)'
                    END AS tier
                FROM '{self.poly_markets}/*.parquet'
                WHERE closed = true AND volume > 0
                  AND outcome_prices IS NOT NULL
                  AND json_extract(outcome_prices, '$[0]') IS NOT NULL
            """).df()
            poly = poly.dropna(subset=['result'])

        df = pd.concat([kalshi, poly], ignore_index=True)
        if df.empty:
            fig, ax = plt.subplots()
            ax.text(0.5, 0.5, "No data", ha='center', va='center', transform=ax.transAxes)
            return AnalysisOutput(figure=fig, data=pd.DataFrame())

        with self.progress("Computing tier profitability"):
            # For each tier, compute: avg prediction error, favorite win rate, calibration
            df['pred_error'] = np.abs(df['price'] - df['resolved_yes'])
            df['favored_won'] = (
                ((df['price'] > 0.5) & (df['resolved_yes'] == 1)) |
                ((df['price'] <= 0.5) & (df['resolved_yes'] == 0))
            ).astype(int)

            tier_order = ['Micro (<100)', 'Small (100-1K)', 'Medium (1K-10K)',
                         'Large (10K-100K)', 'Whale (100K+)']

            stats = df.groupby('tier').agg(
                markets=('volume', 'count'),
                avg_volume=('volume', 'mean'),
                avg_error=('pred_error', 'mean'),
                favorite_wr=('favored_won', lambda x: 100 * x.mean()),
                yes_rate=('resolved_yes', 'mean'),
                avg_price=('price', 'mean'),
            ).reindex(tier_order).dropna()

            # "Edge" = how much better than random (50%) the favorite wins
            stats['edge_pp'] = stats['favorite_wr'] - 50

        metadata = {
            'total_markets': len(df),
            'platforms': list(df['platform'].unique()),
            'whale_markets': int((df['tier'] == 'Whale (100K+)').sum()),
        }

        fig = self._create_figure(stats, tier_order, metadata)
        return AnalysisOutput(figure=fig, data=stats.reset_index(), metadata=metadata)

    def _create_figure(self, stats, tier_order, metadata):
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        colors = ['#8888a0', '#6c5ce7', '#00e676', '#ffab40', '#ff5252']
        tiers = stats.index.tolist()
        tc = {t: c for t, c in zip(tier_order, colors)}

        ax = axes[0, 0]
        bars = ax.bar(range(len(tiers)), stats['favorite_wr'],
                     color=[tc.get(t, '#999') for t in tiers], alpha=0.8)
        ax.set_xticks(range(len(tiers)))
        ax.set_xticklabels([t.split('(')[0].strip() for t in tiers], rotation=15)
        ax.set_ylabel("Favorite Win Rate %")
        ax.set_title("Price Accuracy by Volume Tier")
        ax.axhline(y=50, color='#ff5252', linestyle='--', alpha=0.5)
        for bar, val in zip(bars, stats['favorite_wr']):
            ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 0.5,
                    f'{val:.1f}%', ha='center', va='bottom', fontsize=9, fontweight='bold')

        ax = axes[0, 1]
        ax.bar(range(len(tiers)), stats['avg_error'],
               color=[tc.get(t, '#999') for t in tiers], alpha=0.8)
        ax.set_xticks(range(len(tiers)))
        ax.set_xticklabels([t.split('(')[0].strip() for t in tiers], rotation=15)
        ax.set_ylabel("Avg Prediction Error")
        ax.set_title("Pricing Error by Volume Tier")

        ax = axes[1, 0]
        ax.bar(range(len(tiers)), stats['markets'],
               color=[tc.get(t, '#999') for t in tiers], alpha=0.8)
        ax.set_xticks(range(len(tiers)))
        ax.set_xticklabels([t.split('(')[0].strip() for t in tiers], rotation=15)
        ax.set_ylabel("Market Count")
        ax.set_title("Markets by Volume Tier")
        ax.set_yscale('log')

        ax = axes[1, 1]
        ax.axis('off')
        text = f"Volume Tier Profitability\n{'='*40}\n\n"
        for tier in tiers:
            r = stats.loc[tier]
            text += (f"{tier}\n"
                     f"  Markets: {r['markets']:,.0f} | Fav WR: {r['favorite_wr']:.1f}%\n"
                     f"  Edge: {r['edge_pp']:+.1f}pp | Error: {r['avg_error']:.4f}\n\n")
        text += f"Total: {metadata['total_markets']:,} markets"
        ax.text(0.05, 0.95, text, transform=ax.transAxes, fontsize=9,
                verticalalignment='top', fontfamily='monospace',
                bbox=dict(boxstyle='round,pad=0.8', facecolor='#1a1a26', edgecolor='#6c5ce7', alpha=0.95, linewidth=1.5))

        plt.suptitle("Market Profitability by Volume Tier (Whale Proxy)", fontsize=14, fontweight='bold')
        plt.tight_layout()
        return fig
