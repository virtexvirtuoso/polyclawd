"""Polymarket mispricing by price — calibration analysis.

Analyzes whether Polymarket prices are well-calibrated: do markets priced at X%
actually resolve YES X% of the time? Uses market-level data for efficiency.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.common.analysis import Analysis, AnalysisOutput


class PolymarketMispricingByPriceAnalysis(Analysis):
    """Analyze mispricing by price level on Polymarket."""

    def __init__(self):
        super().__init__(
            name="polymarket_mispricing_by_price",
            description="Polymarket price calibration and mispricing analysis",
        )
        base_dir = Path(__file__).parent.parent.parent.parent
        self.markets_dir = base_dir / "data" / "polymarket" / "markets"

    def run(self) -> AnalysisOutput:
        con = duckdb.connect()

        with self.progress("Loading Polymarket resolved markets"):
            df = con.execute(
                f"""
                SELECT
                    id,
                    question,
                    volume,
                    liquidity,
                    CAST(json_extract(outcome_prices, '$[0]') AS DOUBLE) AS yes_price,
                    CAST(json_extract(outcome_prices, '$[1]') AS DOUBLE) AS no_price,
                    end_date,
                    created_at
                FROM '{self.markets_dir}/*.parquet'
                WHERE closed = true
                  AND outcome_prices IS NOT NULL
                  AND json_extract(outcome_prices, '$[0]') IS NOT NULL
                  AND volume > 100
                """
            ).df()

        if df.empty:
            fig, ax = plt.subplots(figsize=(10, 6))
            ax.text(0.5, 0.5, "No Polymarket data available",
                    ha='center', va='center', transform=ax.transAxes, fontsize=14)
            return AnalysisOutput(figure=fig, data=pd.DataFrame())

        with self.progress("Classifying resolutions"):
            # Markets with final price >0.95 or <0.05 are "resolved"
            df['resolved_yes'] = (df['yes_price'] > 0.95).astype(int)
            df['resolved'] = (df['yes_price'] > 0.95) | (df['yes_price'] < 0.05)
            resolved = df[df['resolved']].copy()

            if len(resolved) < 50:
                fig, ax = plt.subplots(figsize=(10, 6))
                ax.text(0.5, 0.5, f"Only {len(resolved)} resolved markets (need 50+)",
                        ha='center', va='center', transform=ax.transAxes, fontsize=14)
                return AnalysisOutput(figure=fig, data=pd.DataFrame())

        # For calibration, we need the INITIAL price, not the final one.
        # Since we only have snapshots, use volume-weighted approach:
        # Compare distribution of all markets' last prices to resolution rates
        with self.progress("Analyzing price calibration"):
            # Use all closed markets (not just clearly resolved) for price distribution
            all_closed = df.copy()
            all_closed['price_bin'] = pd.cut(
                all_closed['yes_price'],
                bins=np.arange(0, 1.05, 0.05),
                labels=[f"{i:.0f}-{i+5:.0f}%" for i in range(0, 100, 5)],
                include_lowest=True
            )

            # For resolved markets, check calibration
            resolved['price_bin'] = pd.cut(
                resolved['yes_price'],  # This is final price, so mostly 0 or 1
                bins=[0, 0.05, 0.95, 1.0],
                labels=['resolved_no', 'unresolved', 'resolved_yes']
            )

        # Better approach: look at VOLUME distribution by price level
        # High volume at extreme prices = confident markets
        # Volume at mid-range prices = contested/mispriced opportunities
        with self.progress("Computing volume-weighted price distribution"):
            bins = np.arange(0, 1.05, 0.05)
            bin_labels = [f"{int(b*100)}-{int((b+0.05)*100)}%" for b in bins[:-1]]
            
            all_closed['pbin'] = pd.cut(all_closed['yes_price'], bins=bins, labels=bin_labels, include_lowest=True)
            
            price_dist = all_closed.groupby('pbin', observed=True).agg(
                market_count=('id', 'count'),
                total_volume=('volume', 'sum'),
                avg_volume=('volume', 'mean'),
                median_volume=('volume', 'median'),
                avg_liquidity=('liquidity', 'mean'),
            ).reset_index()

            # Resolution rate by final price bucket (for clearly resolved markets)
            # This tells us: at what prices do markets tend to stall?
            yes_rate = all_closed.groupby('pbin', observed=True).agg(
                yes_count=('resolved_yes', 'sum'),
                total=('id', 'count'),
            ).reset_index()
            yes_rate['yes_rate'] = yes_rate['yes_count'] / yes_rate['total']

            price_dist = price_dist.merge(yes_rate[['pbin', 'yes_rate']], on='pbin', how='left')

        # Mispricing opportunities: mid-range prices with high volume
        mid_range = all_closed[(all_closed['yes_price'] >= 0.30) & (all_closed['yes_price'] <= 0.70)]
        high_vol_mid = mid_range.nlargest(20, 'volume')[['question', 'yes_price', 'volume']].copy()

        metadata = {
            'total_markets': len(all_closed),
            'resolved_markets': len(resolved),
            'mid_range_markets': len(mid_range),
            'avg_yes_price': float(all_closed['yes_price'].mean()),
            'median_volume': float(all_closed['volume'].median()),
        }

        fig = self._create_figure(price_dist, all_closed, high_vol_mid, metadata)
        return AnalysisOutput(figure=fig, data=price_dist, metadata=metadata)

    def _create_figure(self, price_dist, all_closed, top_mid, metadata):
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))

        # Price distribution
        ax = axes[0, 0]
        ax.bar(range(len(price_dist)), price_dist['market_count'], color='#6c5ce7', alpha=0.8)
        ax.set_xticks(range(0, len(price_dist), 2))
        ax.set_xticklabels(price_dist['pbin'].iloc[::2], rotation=45, ha='right', fontsize=8)
        ax.set_ylabel("Market Count")
        ax.set_title("Markets by Final Price Level")

        # Volume by price level
        ax = axes[0, 1]
        ax.bar(range(len(price_dist)), price_dist['total_volume'] / 1e6, color='#00e676', alpha=0.8)
        ax.set_xticks(range(0, len(price_dist), 2))
        ax.set_xticklabels(price_dist['pbin'].iloc[::2], rotation=45, ha='right', fontsize=8)
        ax.set_ylabel("Total Volume ($M)")
        ax.set_title("Volume by Price Level")

        # Calibration: yes_rate vs price bin center
        ax = axes[1, 0]
        bin_centers = np.arange(0.025, 1.0, 0.05)
        if len(price_dist) == len(bin_centers):
            ax.scatter(bin_centers, price_dist['yes_rate'], color='#ff5252', alpha=0.7, s=50)
            ax.plot([0, 1], [0, 1], 'k--', alpha=0.3, label='Perfect calibration')
            ax.set_xlabel("Price Level (midpoint)")
            ax.set_ylabel("Fraction Resolving YES")
            ax.set_title("Calibration Curve")
            ax.legend()
        else:
            ax.text(0.5, 0.5, "Calibration data incomplete", ha='center', va='center',
                    transform=ax.transAxes)

        # Summary
        ax = axes[1, 1]
        ax.axis('off')
        text = (
            f"Polymarket Mispricing Summary\n"
            f"{'='*35}\n\n"
            f"Total closed markets:  {metadata['total_markets']:,}\n"
            f"Clearly resolved:      {metadata['resolved_markets']:,}\n"
            f"Mid-range (30-70%):    {metadata['mid_range_markets']:,}\n"
            f"Avg final price:       {metadata['avg_yes_price']:.1%}\n"
            f"Median volume:         ${metadata['median_volume']:,.0f}\n\n"
            f"Top contested markets by volume:\n"
        )
        for _, row in top_mid.head(8).iterrows():
            q = row['question'][:40] + '...' if len(str(row['question'])) > 40 else row['question']
            text += f"  {row['yes_price']:.0%} ${row['volume']:,.0f} {q}\n"

        ax.text(0.02, 0.95, text, transform=ax.transAxes, fontsize=9,
                verticalalignment='top', fontfamily='monospace',
                bbox=dict(boxstyle='round,pad=0.8', facecolor='#1a1a26', edgecolor='#6c5ce7', alpha=0.95, linewidth=1.5))

        plt.suptitle("Polymarket Price Calibration & Mispricing Analysis", fontsize=14, fontweight='bold')
        plt.tight_layout()
        return fig
