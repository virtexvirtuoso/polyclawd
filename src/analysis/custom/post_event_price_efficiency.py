"""Post-event price efficiency — how fast do markets react?

Measures how quickly markets move to extreme prices near resolution.
Markets that stay at mid-range prices close to their end date are opportunities.
"""

from __future__ import annotations
from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.common.analysis import Analysis, AnalysisOutput


class PostEventPriceEfficiencyAnalysis(Analysis):
    """Analyze market efficiency near resolution time."""

    def __init__(self):
        super().__init__(
            name="post_event_price_efficiency",
            description="Price efficiency near resolution — reaction window analysis",
        )
        base_dir = Path(__file__).parent.parent.parent.parent
        self.kalshi_markets = base_dir / "data" / "kalshi" / "markets"
        self.poly_markets = base_dir / "data" / "polymarket" / "markets"

    def run(self) -> AnalysisOutput:
        con = duckdb.connect()

        with self.progress("Loading Kalshi markets with timing"):
            kalshi = con.execute(f"""
                SELECT 'kalshi' AS platform,
                    last_price/100.0 AS final_price,
                    CASE WHEN result='yes' THEN 1.0 ELSE 0.0 END AS resolved_yes,
                    volume,
                    EXTRACT(EPOCH FROM (close_time - created_time))/86400.0 AS total_days,
                    created_time, close_time
                FROM '{self.kalshi_markets}/*.parquet'
                WHERE status='finalized' AND result IN ('yes','no')
                  AND last_price BETWEEN 1 AND 99
                  AND close_time IS NOT NULL AND created_time IS NOT NULL
                  AND close_time > created_time AND volume > 0
            """).df()

        with self.progress("Loading Polymarket markets with timing"):
            poly = con.execute(f"""
                SELECT 'polymarket' AS platform,
                    CAST(json_extract(outcome_prices,'$[0]') AS DOUBLE) AS final_price,
                    CASE WHEN CAST(json_extract(outcome_prices,'$[0]') AS DOUBLE) > 0.95 THEN 1.0
                         WHEN CAST(json_extract(outcome_prices,'$[0]') AS DOUBLE) < 0.05 THEN 0.0
                         ELSE NULL END AS resolved_yes,
                    volume,
                    EXTRACT(EPOCH FROM (end_date - created_at))/86400.0 AS total_days,
                    created_at AS created_time, end_date AS close_time
                FROM '{self.poly_markets}/*.parquet'
                WHERE closed=true AND outcome_prices IS NOT NULL
                  AND json_extract(outcome_prices,'$[0]') IS NOT NULL
                  AND end_date IS NOT NULL AND created_at IS NOT NULL
                  AND end_date > created_at AND volume > 100
            """).df()
            poly = poly.dropna(subset=['resolved_yes'])

        df = pd.concat([kalshi, poly], ignore_index=True)
        df = df.dropna(subset=['total_days'])
        df = df[df['total_days'] > 0]

        if len(df) < 100:
            fig, ax = plt.subplots()
            ax.text(0.5, 0.5, "Insufficient data", ha='center', va='center', transform=ax.transAxes)
            return AnalysisOutput(figure=fig, data=pd.DataFrame())

        with self.progress("Analyzing efficiency patterns"):
            df['confidence'] = np.abs(df['final_price'] - 0.5) * 2
            df['is_extreme'] = (df['confidence'] > 0.9).astype(int)  # >95% or <5%
            df['is_contested'] = (df['confidence'] < 0.4).astype(int)  # 30-70%

            # Group by total market duration
            dur_bins = [0, 1, 3, 7, 14, 30, 90, 365, 10000]
            dur_labels = ['<1d', '1-3d', '3-7d', '1-2w', '2-4w', '1-3mo', '3-12mo', '1y+']
            df['dur_bin'] = pd.cut(df['total_days'], bins=dur_bins, labels=dur_labels)

            efficiency = df.groupby('dur_bin', observed=True).agg(
                markets=('volume', 'count'),
                pct_extreme=('is_extreme', lambda x: 100 * x.mean()),
                pct_contested=('is_contested', lambda x: 100 * x.mean()),
                avg_confidence=('confidence', 'mean'),
                avg_volume=('volume', 'mean'),
            ).reset_index()

            # Platform comparison
            plat_eff = df.groupby('platform').agg(
                markets=('volume', 'count'),
                pct_extreme=('is_extreme', lambda x: 100 * x.mean()),
                pct_contested=('is_contested', lambda x: 100 * x.mean()),
                avg_confidence=('confidence', 'mean'),
            ).reset_index()

        metadata = {
            'total_markets': len(df),
            'overall_extreme_pct': float(df['is_extreme'].mean() * 100),
            'overall_contested_pct': float(df['is_contested'].mean() * 100),
            'short_contested': float(df[df['total_days'] <= 7]['is_contested'].mean() * 100),
            'long_contested': float(df[df['total_days'] > 90]['is_contested'].mean() * 100),
        }

        fig = self._create_figure(efficiency, plat_eff, metadata)
        return AnalysisOutput(figure=fig, data=efficiency, metadata=metadata)

    def _create_figure(self, eff, plat, metadata):
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        colors = ['#ff5252', '#ffab40', '#ffd700', '#00e676', '#6c5ce7', '#a29bfe', '#18ffff', '#8888a0']

        ax = axes[0, 0]
        ax.bar(range(len(eff)), eff['pct_extreme'], color=colors[:len(eff)], alpha=0.8)
        ax.set_xticks(range(len(eff)))
        ax.set_xticklabels(eff['dur_bin'], rotation=30)
        ax.set_ylabel("% Markets at Extreme Price")
        ax.set_title("Price Extremity by Market Duration")

        ax = axes[0, 1]
        ax.bar(range(len(eff)), eff['pct_contested'], color=colors[:len(eff)], alpha=0.8)
        ax.set_xticks(range(len(eff)))
        ax.set_xticklabels(eff['dur_bin'], rotation=30)
        ax.set_ylabel("% Markets in Contested Range")
        ax.set_title("Contested Markets (30-70%) by Duration")

        ax = axes[1, 0]
        for _, row in plat.iterrows():
            ax.bar(row['platform'], row['pct_contested'],
                   color='#ff5252' if row['platform'] == 'kalshi' else '#3498db', alpha=0.8)
        ax.set_ylabel("% Contested at Close")
        ax.set_title("Platform Efficiency Comparison")

        ax = axes[1, 1]
        ax.axis('off')
        text = (
            f"Price Efficiency Summary\n{'='*35}\n\n"
            f"Total markets:       {metadata['total_markets']:,}\n"
            f"At extreme (>95%):   {metadata['overall_extreme_pct']:.1f}%\n"
            f"Contested (30-70%):  {metadata['overall_contested_pct']:.1f}%\n\n"
            f"Short-term contested: {metadata['short_contested']:.1f}%\n"
            f"Long-term contested:  {metadata['long_contested']:.1f}%\n\n"
            f"Insight: {'Short markets more efficient' if metadata['short_contested'] < metadata['long_contested'] else 'Long markets more efficient'}"
        )
        ax.text(0.05, 0.95, text, transform=ax.transAxes, fontsize=11,
                verticalalignment='top', fontfamily='monospace',
                bbox=dict(boxstyle='round,pad=0.8', facecolor='#1a1a26', edgecolor='#6c5ce7', alpha=0.95, linewidth=1.5))

        plt.suptitle("Post-Event Price Efficiency — Reaction Window Analysis", fontsize=14, fontweight='bold')
        plt.tight_layout()
        return fig
