"""Weekend vs weekday efficiency — are markets less efficient on weekends?

Prediction markets may have reduced participation on weekends, creating
pricing inefficiencies. Compares calibration by day of week.
"""

from __future__ import annotations
from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.common.analysis import Analysis, AnalysisOutput


class WeekendVsWeekdayEfficiencyAnalysis(Analysis):
    """Compare market efficiency between weekends and weekdays."""

    def __init__(self):
        super().__init__(
            name="weekend_vs_weekday_efficiency",
            description="Weekend vs weekday pricing efficiency comparison",
        )
        base_dir = Path(__file__).parent.parent.parent.parent
        self.kalshi_markets = base_dir / "data" / "kalshi" / "markets"
        self.poly_markets = base_dir / "data" / "polymarket" / "markets"

    def run(self) -> AnalysisOutput:
        con = duckdb.connect()

        with self.progress("Loading Kalshi markets with day-of-week"):
            kalshi = con.execute(f"""
                SELECT 'kalshi' AS platform,
                    last_price/100.0 AS price,
                    CASE WHEN result='yes' THEN 1.0 ELSE 0.0 END AS resolved_yes,
                    volume,
                    EXTRACT(DOW FROM close_time) AS close_dow,
                    EXTRACT(HOUR FROM close_time) AS close_hour,
                    close_time
                FROM '{self.kalshi_markets}/*.parquet'
                WHERE status='finalized' AND result IN ('yes','no')
                  AND last_price BETWEEN 1 AND 99
                  AND close_time IS NOT NULL AND volume > 0
            """).df()

        with self.progress("Loading Polymarket markets with day-of-week"):
            poly = con.execute(f"""
                SELECT 'polymarket' AS platform,
                    CAST(json_extract(outcome_prices,'$[0]') AS DOUBLE) AS price,
                    CASE WHEN CAST(json_extract(outcome_prices,'$[0]') AS DOUBLE) > 0.95 THEN 1.0
                         WHEN CAST(json_extract(outcome_prices,'$[0]') AS DOUBLE) < 0.05 THEN 0.0
                         ELSE NULL END AS resolved_yes,
                    volume,
                    EXTRACT(DOW FROM end_date) AS close_dow,
                    EXTRACT(HOUR FROM end_date) AS close_hour,
                    end_date AS close_time
                FROM '{self.poly_markets}/*.parquet'
                WHERE closed=true AND outcome_prices IS NOT NULL
                  AND json_extract(outcome_prices,'$[0]') IS NOT NULL
                  AND end_date IS NOT NULL AND volume > 100
            """).df()
            poly = poly.dropna(subset=['resolved_yes'])

        df = pd.concat([kalshi, poly], ignore_index=True)
        df = df.dropna(subset=['close_dow'])

        if len(df) < 100:
            fig, ax = plt.subplots()
            ax.text(0.5, 0.5, "Insufficient data", ha='center', va='center', transform=ax.transAxes)
            return AnalysisOutput(figure=fig, data=pd.DataFrame())

        with self.progress("Computing day-of-week efficiency"):
            df['pred_error'] = np.abs(df['price'] - df['resolved_yes'])
            df['correct'] = (
                ((df['price'] > 0.5) & (df['resolved_yes'] == 1)) |
                ((df['price'] <= 0.5) & (df['resolved_yes'] == 0))
            ).astype(int)
            df['is_weekend'] = df['close_dow'].isin([0, 6]).astype(int)  # Sun=0, Sat=6

            day_names = {0: 'Sun', 1: 'Mon', 2: 'Tue', 3: 'Wed', 4: 'Thu', 5: 'Fri', 6: 'Sat'}
            df['day_name'] = df['close_dow'].map(day_names)

            dow_stats = df.groupby('close_dow').agg(
                day=('day_name', 'first'),
                markets=('volume', 'count'),
                avg_error=('pred_error', 'mean'),
                accuracy=('correct', lambda x: 100 * x.mean()),
                avg_volume=('volume', 'mean'),
            ).sort_index().reset_index()

            # Weekend vs weekday aggregate
            wk_stats = df.groupby('is_weekend').agg(
                markets=('volume', 'count'),
                avg_error=('pred_error', 'mean'),
                accuracy=('correct', lambda x: 100 * x.mean()),
            ).reset_index()

            # By hour
            hour_stats = df.groupby('close_hour').agg(
                markets=('volume', 'count'),
                avg_error=('pred_error', 'mean'),
                accuracy=('correct', lambda x: 100 * x.mean()),
            ).reset_index()

        weekend_err = float(wk_stats[wk_stats['is_weekend']==1]['avg_error'].iloc[0]) if len(wk_stats[wk_stats['is_weekend']==1]) > 0 else 0
        weekday_err = float(wk_stats[wk_stats['is_weekend']==0]['avg_error'].iloc[0]) if len(wk_stats[wk_stats['is_weekend']==0]) > 0 else 0

        metadata = {
            'total_markets': len(df),
            'weekend_error': weekend_err,
            'weekday_error': weekday_err,
            'weekend_edge': weekend_err - weekday_err,
            'worst_day': dow_stats.loc[dow_stats['avg_error'].idxmax(), 'day'] if not dow_stats.empty else 'N/A',
            'best_day': dow_stats.loc[dow_stats['avg_error'].idxmin(), 'day'] if not dow_stats.empty else 'N/A',
        }

        fig = self._create_figure(dow_stats, hour_stats, wk_stats, metadata)
        return AnalysisOutput(figure=fig, data=dow_stats, metadata=metadata)

    def _create_figure(self, dow, hours, wk, metadata):
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))

        day_colors = ['#6c5ce7', '#18ffff', '#18ffff', '#18ffff', '#18ffff', '#18ffff', '#6c5ce7']

        ax = axes[0, 0]
        ax.bar(range(len(dow)), dow['avg_error'], color=day_colors[:len(dow)], alpha=0.8)
        ax.set_xticks(range(len(dow)))
        ax.set_xticklabels(dow['day'])
        ax.set_ylabel("Avg Prediction Error")
        ax.set_title("Pricing Error by Day of Week")
        ax.axhline(y=dow['avg_error'].mean(), color='#8888a0', linestyle='--', alpha=0.5)

        ax = axes[0, 1]
        ax.bar(range(len(dow)), dow['accuracy'], color=day_colors[:len(dow)], alpha=0.8)
        ax.set_xticks(range(len(dow)))
        ax.set_xticklabels(dow['day'])
        ax.set_ylabel("Accuracy %")
        ax.set_title("Price Accuracy by Day")
        ax.axhline(y=50, color='#ff5252', linestyle='--', alpha=0.3)

        ax = axes[1, 0]
        if not hours.empty:
            ax.plot(hours['close_hour'], hours['avg_error'], 'o-', color='#a29bfe', alpha=0.8)
            ax.set_xlabel("Hour (UTC)")
            ax.set_ylabel("Avg Error")
            ax.set_title("Pricing Error by Hour of Day")
            ax.set_xlim(0, 23)

        ax = axes[1, 1]
        ax.axis('off')
        edge_dir = "MORE" if metadata['weekend_edge'] > 0 else "LESS"
        text = (
            f"Weekend vs Weekday Summary\n{'='*35}\n\n"
            f"Total markets:     {metadata['total_markets']:,}\n\n"
            f"Weekend error:     {metadata['weekend_error']:.5f}\n"
            f"Weekday error:     {metadata['weekday_error']:.5f}\n"
            f"Difference:        {metadata['weekend_edge']:+.5f}\n\n"
            f"Worst day:         {metadata['worst_day']}\n"
            f"Best day:          {metadata['best_day']}\n\n"
            f"Conclusion: Weekends are {edge_dir}\n"
            f"mispriced ({abs(metadata['weekend_edge']):.5f} diff)"
        )
        ax.text(0.05, 0.95, text, transform=ax.transAxes, fontsize=11,
                verticalalignment='top', fontfamily='monospace',
                bbox=dict(boxstyle='round,pad=0.8', facecolor='#1a1a26', edgecolor='#6c5ce7', alpha=0.95, linewidth=1.5))

        plt.suptitle("Weekend vs Weekday Market Efficiency", fontsize=14, fontweight='bold')
        plt.tight_layout()
        return fig
