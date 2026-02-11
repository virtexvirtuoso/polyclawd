"""Resolution timing profit — theta collection backtest.

Markets that resolve predictably (high confidence near close) offer theta-like
returns. Analyzes the relationship between market age, price extremity at close,
and implied theta value. Helps Polyclawd with resolution_timing signals.
"""

from __future__ import annotations
from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.common.analysis import Analysis, AnalysisOutput


class ResolutionTimingProfitAnalysis(Analysis):
    """Analyze theta-like returns from resolution timing patterns."""

    def __init__(self):
        super().__init__(
            name="resolution_timing_profit",
            description="Theta collection from resolution timing patterns",
        )
        base_dir = Path(__file__).parent.parent.parent.parent
        self.kalshi_markets = base_dir / "data" / "kalshi" / "markets"
        self.poly_markets = base_dir / "data" / "polymarket" / "markets"

    def run(self) -> AnalysisOutput:
        con = duckdb.connect()

        with self.progress("Loading Kalshi markets with timing data"):
            kalshi = con.execute(f"""
                SELECT 'kalshi' AS platform,
                    last_price/100.0 AS final_price,
                    result,
                    volume,
                    CASE WHEN result='yes' THEN 1.0 ELSE 0.0 END AS resolved_yes,
                    EXTRACT(EPOCH FROM (close_time - created_time))/86400.0 AS market_days,
                    EXTRACT(DOW FROM close_time) AS close_dow,
                    EXTRACT(HOUR FROM close_time) AS close_hour
                FROM '{self.kalshi_markets}/*.parquet'
                WHERE status='finalized' AND result IN ('yes','no')
                  AND last_price BETWEEN 1 AND 99
                  AND close_time IS NOT NULL AND created_time IS NOT NULL
                  AND close_time > created_time
            """).df()

        with self.progress("Loading Polymarket markets with timing data"):
            poly = con.execute(f"""
                SELECT 'polymarket' AS platform,
                    CAST(json_extract(outcome_prices,'$[0]') AS DOUBLE) AS final_price,
                    CASE WHEN CAST(json_extract(outcome_prices,'$[0]') AS DOUBLE) > 0.95 THEN 'yes'
                         WHEN CAST(json_extract(outcome_prices,'$[0]') AS DOUBLE) < 0.05 THEN 'no'
                         ELSE NULL END AS result,
                    volume,
                    CASE WHEN CAST(json_extract(outcome_prices,'$[0]') AS DOUBLE) > 0.95 THEN 1.0 ELSE 0.0 END AS resolved_yes,
                    EXTRACT(EPOCH FROM (end_date - created_at))/86400.0 AS market_days,
                    EXTRACT(DOW FROM end_date) AS close_dow,
                    EXTRACT(HOUR FROM end_date) AS close_hour
                FROM '{self.poly_markets}/*.parquet'
                WHERE closed=true AND outcome_prices IS NOT NULL
                  AND json_extract(outcome_prices,'$[0]') IS NOT NULL
                  AND end_date IS NOT NULL AND created_at IS NOT NULL
                  AND end_date > created_at AND volume > 100
            """).df()
            poly = poly.dropna(subset=['result'])

        df = pd.concat([kalshi, poly], ignore_index=True)
        df = df.dropna(subset=['market_days'])
        df = df[df['market_days'] > 0]

        if len(df) < 100:
            fig, ax = plt.subplots()
            ax.text(0.5, 0.5, "Insufficient data", ha='center', va='center', transform=ax.transAxes)
            return AnalysisOutput(figure=fig, data=pd.DataFrame())

        with self.progress("Computing theta metrics"):
            # Theta proxy: markets at extreme prices that resolve correctly
            df['confidence'] = np.abs(df['final_price'] - 0.5) * 2  # 0=50/50, 1=certain
            df['correct'] = (
                ((df['final_price'] > 0.5) & (df['resolved_yes'] == 1)) |
                ((df['final_price'] <= 0.5) & (df['resolved_yes'] == 0))
            ).astype(int)

            # Theta = confidence * correct / market_days (higher = better theta)
            df['theta_proxy'] = df['confidence'] * df['correct'] / df['market_days'].clip(lower=1)

            # Bin by market duration
            duration_bins = [0, 1, 7, 30, 90, 365, 10000]
            duration_labels = ['<1d', '1-7d', '1-4w', '1-3mo', '3-12mo', '1y+']
            df['duration_bin'] = pd.cut(df['market_days'], bins=duration_bins, labels=duration_labels)

            timing = df.groupby('duration_bin', observed=True).agg(
                markets=('volume', 'count'),
                avg_confidence=('confidence', 'mean'),
                accuracy=('correct', lambda x: 100 * x.mean()),
                avg_theta=('theta_proxy', 'mean'),
                avg_volume=('volume', 'mean'),
            ).reset_index()

        metadata = {
            'total_markets': len(df),
            'best_theta_bucket': timing.loc[timing['avg_theta'].idxmax(), 'duration_bin'] if not timing.empty else 'N/A',
            'short_term_accuracy': float(df[df['market_days'] <= 7]['correct'].mean() * 100) if len(df[df['market_days'] <= 7]) > 0 else 0,
            'long_term_accuracy': float(df[df['market_days'] > 90]['correct'].mean() * 100) if len(df[df['market_days'] > 90]) > 0 else 0,
        }

        fig = self._create_figure(df, timing, metadata)
        return AnalysisOutput(figure=fig, data=timing, metadata=metadata)

    def _create_figure(self, df, timing, metadata):
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        colors = ['#ff5252', '#ffab40', '#00e676', '#6c5ce7', '#a29bfe', '#18ffff']

        ax = axes[0, 0]
        ax.bar(range(len(timing)), timing['accuracy'],
               color=colors[:len(timing)], alpha=0.8)
        ax.set_xticks(range(len(timing)))
        ax.set_xticklabels(timing['duration_bin'])
        ax.set_ylabel("Accuracy %")
        ax.set_title("Price Accuracy by Market Duration")
        ax.axhline(y=50, color='#ff5252', linestyle='--', alpha=0.5)

        ax = axes[0, 1]
        ax.bar(range(len(timing)), timing['avg_theta'],
               color=colors[:len(timing)], alpha=0.8)
        ax.set_xticks(range(len(timing)))
        ax.set_xticklabels(timing['duration_bin'])
        ax.set_ylabel("Theta Proxy (confidence/day)")
        ax.set_title("Theta Value by Duration")

        ax = axes[1, 0]
        sample = df.sample(min(5000, len(df)), random_state=42)
        ax.scatter(sample['market_days'].clip(upper=365), sample['confidence'],
                  alpha=0.05, s=2, color='#6c5ce7')
        ax.set_xlabel("Market Duration (days)")
        ax.set_ylabel("Final Confidence")
        ax.set_title("Confidence vs Duration")
        ax.set_xlim(0, 365)

        ax = axes[1, 1]
        ax.axis('off')
        text = (
            f"Resolution Timing Summary\n{'='*35}\n\n"
            f"Total markets:       {metadata['total_markets']:,}\n"
            f"Best theta bucket:   {metadata['best_theta_bucket']}\n\n"
            f"Short-term (≤7d):    {metadata['short_term_accuracy']:.1f}% accuracy\n"
            f"Long-term (>90d):    {metadata['long_term_accuracy']:.1f}% accuracy\n\n"
        )
        for _, row in timing.iterrows():
            text += f"  {row['duration_bin']:8s}: {row['accuracy']:.1f}% acc, θ={row['avg_theta']:.4f}\n"
        ax.text(0.05, 0.95, text, transform=ax.transAxes, fontsize=10,
                verticalalignment='top', fontfamily='monospace',
                bbox=dict(boxstyle='round,pad=0.8', facecolor='#1a1a26', edgecolor='#6c5ce7', alpha=0.95, linewidth=1.5))

        plt.suptitle("Resolution Timing Profit — Theta Collection Analysis", fontsize=14, fontweight='bold')
        plt.tight_layout()
        return fig
