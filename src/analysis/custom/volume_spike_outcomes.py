"""Volume spike outcome analysis.

Analyzes whether high-volume markets (volume outliers) resolve differently than
low-volume ones. Validates whether Polyclawd's volume_spike signal has predictive edge.
Uses market-level volume data (works without trade-level data).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.common.analysis import Analysis, AnalysisOutput


class VolumeSpikeOutcomesAnalysis(Analysis):
    """Analyze whether high-volume markets show pricing efficiency differences."""

    def __init__(self):
        super().__init__(
            name="volume_spike_outcomes",
            description="Do volume spikes predict outcomes? Validates volume_spike signal",
        )
        base_dir = Path(__file__).parent.parent.parent.parent
        self.kalshi_markets = base_dir / "data" / "kalshi" / "markets"
        self.poly_markets = base_dir / "data" / "polymarket" / "markets"

    def run(self) -> AnalysisOutput:
        con = duckdb.connect()

        # Kalshi: resolved markets with volume and last_price
        with self.progress("Loading Kalshi resolved markets"):
            kalshi_df = con.execute(
                f"""
                SELECT
                    ticker AS market_id,
                    'kalshi' AS platform,
                    volume,
                    last_price / 100.0 AS price,
                    result,
                    CASE WHEN result = 'yes' THEN 1 ELSE 0 END AS resolved_yes
                FROM '{self.kalshi_markets}/*.parquet'
                WHERE status = 'finalized'
                  AND result IN ('yes', 'no')
                  AND last_price IS NOT NULL
                  AND last_price BETWEEN 1 AND 99
                  AND volume > 0
                """
            ).df()

        # Polymarket: closed markets with volume
        with self.progress("Loading Polymarket resolved markets"):
            poly_df = con.execute(
                f"""
                SELECT
                    id AS market_id,
                    'polymarket' AS platform,
                    volume,
                    CAST(json_extract(outcome_prices, '$[0]') AS DOUBLE) AS price,
                    CASE WHEN CAST(json_extract(outcome_prices, '$[0]') AS DOUBLE) > 0.95 THEN 'yes'
                         WHEN CAST(json_extract(outcome_prices, '$[0]') AS DOUBLE) < 0.05 THEN 'no'
                         ELSE NULL END AS result,
                    CASE WHEN CAST(json_extract(outcome_prices, '$[0]') AS DOUBLE) > 0.95 THEN 1 ELSE 0 END AS resolved_yes
                FROM '{self.poly_markets}/*.parquet'
                WHERE closed = true
                  AND volume > 100
                  AND outcome_prices IS NOT NULL
                  AND json_extract(outcome_prices, '$[0]') IS NOT NULL
                """
            ).df()
            # Only keep clearly resolved markets
            poly_df = poly_df.dropna(subset=['result'])

        # Combine
        df = pd.concat([kalshi_df, poly_df], ignore_index=True)
        if df.empty or len(df) < 100:
            fig, ax = plt.subplots(figsize=(10, 6))
            ax.text(0.5, 0.5, "Insufficient resolved market data",
                    ha='center', va='center', transform=ax.transAxes, fontsize=14)
            return AnalysisOutput(figure=fig, data=pd.DataFrame())

        # Compute volume z-scores per platform
        with self.progress("Computing volume spike classifications"):
            results = []
            for platform in df['platform'].unique():
                pdf = df[df['platform'] == platform].copy()
                log_vol = np.log1p(pdf['volume'])
                mean_v = log_vol.mean()
                std_v = log_vol.std()
                if std_v == 0:
                    continue
                pdf['z_score'] = (log_vol - mean_v) / std_v
                pdf['spike_level'] = pd.cut(
                    pdf['z_score'],
                    bins=[-np.inf, 1, 2, 3, 4, np.inf],
                    labels=['<1σ', '1-2σ', '2-3σ', '3-4σ', '4σ+']
                )
                results.append(pdf)
            
            combined = pd.concat(results, ignore_index=True)

        # For each spike level, check if high-volume markets are better calibrated
        # (price closer to outcome) — this is what matters for edge detection
        with self.progress("Analyzing calibration by volume tier"):
            combined['prediction_error'] = np.abs(
                combined['price'] - combined['resolved_yes']
            )
            
            summary = combined.groupby('spike_level', observed=True).agg(
                events=('market_id', 'count'),
                avg_error=('prediction_error', 'mean'),
                median_error=('prediction_error', 'median'),
                yes_rate=('resolved_yes', 'mean'),
                avg_price=('price', 'mean'),
                avg_volume=('volume', 'mean'),
            ).reset_index()

            # "Win rate" = how often the favored side wins in spike markets
            # For high-volume markets, the last price should be MORE accurate
            spike_only = combined[combined['z_score'] >= 1].copy()
            spike_only['favored_won'] = (
                ((spike_only['price'] > 0.5) & (spike_only['resolved_yes'] == 1)) |
                ((spike_only['price'] <= 0.5) & (spike_only['resolved_yes'] == 0))
            ).astype(int)

            win_by_level = spike_only.groupby('spike_level', observed=True).agg(
                spike_events=('market_id', 'count'),
                spike_won=('favored_won', 'sum'),
                win_rate=('favored_won', lambda x: 100.0 * x.mean()),
                avg_z=('z_score', 'mean'),
            ).reset_index()

        metadata = {
            'total_markets': len(combined),
            'spike_markets': len(spike_only),
            'platforms': list(combined['platform'].unique()),
        }

        fig = self._create_figure(summary, win_by_level, metadata)
        return AnalysisOutput(figure=fig, data=win_by_level, metadata=metadata)

    def _create_figure(self, summary, win_df, metadata):
        fig, axes = plt.subplots(1, 3, figsize=(16, 6))

        level_order = ['<1σ', '1-2σ', '2-3σ', '3-4σ', '4σ+']
        colors = ['#8888a0', '#6c5ce7', '#00e676', '#ffab40', '#ff5252']

        # Prediction error by volume tier
        ax = axes[0]
        summary_sorted = summary.set_index('spike_level').reindex(level_order).dropna(subset=['events'])
        ax.bar(range(len(summary_sorted)), summary_sorted['avg_error'],
               color=colors[:len(summary_sorted)], alpha=0.8)
        ax.set_xticks(range(len(summary_sorted)))
        ax.set_xticklabels(summary_sorted.index)
        ax.set_ylabel("Avg Prediction Error")
        ax.set_title("Price Accuracy by Volume Tier")

        # Win rate for spike markets
        ax = axes[1]
        win_sorted = win_df.set_index('spike_level').reindex([l for l in level_order if l != '<1σ']).dropna(subset=['spike_events'])
        if not win_sorted.empty:
            bars = ax.bar(range(len(win_sorted)), win_sorted['win_rate'],
                         color=colors[1:len(win_sorted)+1], alpha=0.8)
            ax.set_xticks(range(len(win_sorted)))
            ax.set_xticklabels(win_sorted.index)
            ax.axhline(y=50, color='#ff5252', linestyle='--', label='Random')
            for bar, val in zip(bars, win_sorted['win_rate']):
                ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 1,
                        f'{val:.1f}%', ha='center', va='bottom', fontsize=10, fontweight='bold')
            ax.legend()
        ax.set_ylabel("Favored Side Win Rate %")
        ax.set_title("Does Volume Confirm the Favorite?")
        ax.set_ylim(0, 100)

        # Summary text
        ax = axes[2]
        ax.axis('off')
        total = win_df['spike_events'].sum()
        total_wins = win_df['spike_won'].sum()
        wr = 100.0 * total_wins / total if total > 0 else 0

        text = (
            f"Volume Spike Signal Summary\n"
            f"{'='*35}\n\n"
            f"Total markets:       {metadata['total_markets']:,}\n"
            f"Spike markets (≥1σ): {metadata['spike_markets']:,}\n"
            f"Overall win rate:    {wr:.1f}%\n"
            f"Edge over random:    {wr - 50:+.1f}pp\n\n"
        )
        for _, row in win_df.iterrows():
            edge = row['win_rate'] - 50
            emoji = "✅" if edge > 2 else "⚠️" if edge > 0 else "❌"
            text += f"{emoji} {row['spike_level']:6s}: {row['win_rate']:.1f}% ({row['spike_events']:,} events)\n"

        text += f"\nConclusion: {'SIGNAL HAS EDGE' if wr > 52 else 'WEAK/NO EDGE'}"

        ax.text(0.05, 0.95, text, transform=ax.transAxes, fontsize=10,
                verticalalignment='top', fontfamily='monospace',
                bbox=dict(boxstyle='round,pad=0.8', facecolor='#1a1a26', edgecolor='#6c5ce7', alpha=0.95, linewidth=1.5))

        plt.suptitle("Volume Spike → Outcome Analysis (Signal Validation)", fontsize=14, fontweight='bold')
        plt.tight_layout()
        return fig
