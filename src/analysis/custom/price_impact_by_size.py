"""Price impact by market size — position sizing guide.

Analyzes how market volume correlates with pricing accuracy. Higher volume markets
should have tighter spreads and better calibration. Helps Polyclawd determine
optimal position sizes relative to market liquidity.
"""

from __future__ import annotations
from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.common.analysis import Analysis, AnalysisOutput


class PriceImpactBySizeAnalysis(Analysis):
    """Analyze pricing efficiency as a function of market volume/liquidity."""

    def __init__(self):
        super().__init__(
            name="price_impact_by_size",
            description="Price accuracy vs market size — position sizing guide",
        )
        base_dir = Path(__file__).parent.parent.parent.parent
        self.kalshi_markets = base_dir / "data" / "kalshi" / "markets"
        self.poly_markets = base_dir / "data" / "polymarket" / "markets"

    def run(self) -> AnalysisOutput:
        con = duckdb.connect()

        with self.progress("Loading resolved markets"):
            kalshi = con.execute(f"""
                SELECT 'kalshi' AS platform, volume, last_price/100.0 AS price,
                    CASE WHEN result='yes' THEN 1.0 ELSE 0.0 END AS resolved_yes,
                    open_interest
                FROM '{self.kalshi_markets}/*.parquet'
                WHERE status='finalized' AND result IN ('yes','no')
                  AND last_price BETWEEN 1 AND 99 AND volume > 0
            """).df()

            poly = con.execute(f"""
                SELECT 'polymarket' AS platform, volume,
                    CAST(json_extract(outcome_prices,'$[0]') AS DOUBLE) AS price,
                    CASE WHEN CAST(json_extract(outcome_prices,'$[0]') AS DOUBLE) > 0.95 THEN 1.0
                         WHEN CAST(json_extract(outcome_prices,'$[0]') AS DOUBLE) < 0.05 THEN 0.0
                         ELSE NULL END AS resolved_yes,
                    liquidity AS open_interest
                FROM '{self.poly_markets}/*.parquet'
                WHERE closed=true AND volume > 0 AND outcome_prices IS NOT NULL
                  AND json_extract(outcome_prices,'$[0]') IS NOT NULL
            """).df()
            poly = poly.dropna(subset=['resolved_yes'])

        df = pd.concat([kalshi, poly], ignore_index=True)
        if len(df) < 100:
            fig, ax = plt.subplots()
            ax.text(0.5, 0.5, "Insufficient data", ha='center', va='center', transform=ax.transAxes)
            return AnalysisOutput(figure=fig, data=pd.DataFrame())

        with self.progress("Computing price impact metrics"):
            df['pred_error'] = np.abs(df['price'] - df['resolved_yes'])
            df['log_volume'] = np.log10(df['volume'].clip(lower=1))

            # Bin by log volume
            bins = np.arange(1, 8, 0.5)  # 10 to 10M
            bin_labels = [f"10^{b:.1f}" for b in bins[:-1]]
            df['vol_bin'] = pd.cut(df['log_volume'], bins=bins, labels=bin_labels)

            impact = df.groupby('vol_bin', observed=True).agg(
                markets=('volume', 'count'),
                avg_error=('pred_error', 'mean'),
                median_error=('pred_error', 'median'),
                avg_volume=('volume', 'mean'),
            ).reset_index()

            # By platform
            plat_impact = df.groupby(['platform', 'vol_bin'], observed=True).agg(
                markets=('volume', 'count'),
                avg_error=('pred_error', 'mean'),
            ).reset_index()

        metadata = {
            'total_markets': len(df),
            'correlation': float(df[['log_volume', 'pred_error']].corr().iloc[0, 1]),
            'avg_error_low_vol': float(df[df['log_volume'] < 3]['pred_error'].mean()),
            'avg_error_high_vol': float(df[df['log_volume'] >= 5]['pred_error'].mean()),
        }

        fig = self._create_figure(df, impact, plat_impact, metadata)
        return AnalysisOutput(figure=fig, data=impact, metadata=metadata)

    def _create_figure(self, df, impact, plat_impact, metadata):
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))

        ax = axes[0, 0]
        sample = df.sample(min(5000, len(df)), random_state=42)
        ax.scatter(sample['log_volume'], sample['pred_error'], alpha=0.1, s=2, color='#6c5ce7')
        ax.set_xlabel("log10(Volume)")
        ax.set_ylabel("Prediction Error")
        ax.set_title(f"Volume vs Pricing Error (r={metadata['correlation']:.3f})")

        ax = axes[0, 1]
        ax.bar(range(len(impact)), impact['avg_error'], color='#ff5252', alpha=0.8)
        ax.set_xticks(range(0, len(impact), 2))
        ax.set_xticklabels(impact['vol_bin'].iloc[::2], rotation=45, fontsize=8)
        ax.set_ylabel("Avg Prediction Error")
        ax.set_title("Pricing Error by Volume Bucket")

        ax = axes[1, 0]
        for plat, color in [('kalshi', '#e74c3c'), ('polymarket', '#3498db')]:
            pdata = plat_impact[plat_impact['platform'] == plat]
            if not pdata.empty:
                ax.plot(range(len(pdata)), pdata['avg_error'], 'o-', label=plat, color=color, alpha=0.8)
        ax.set_xlabel("Volume Bucket")
        ax.set_ylabel("Avg Error")
        ax.set_title("Error by Platform & Volume")
        ax.legend()

        ax = axes[1, 1]
        ax.axis('off')
        text = (
            f"Price Impact Summary\n{'='*35}\n\n"
            f"Total markets:     {metadata['total_markets']:,}\n"
            f"Vol-Error corr:    {metadata['correlation']:.3f}\n\n"
            f"Low vol (<$1K):    {metadata['avg_error_low_vol']:.4f} error\n"
            f"High vol (>$100K): {metadata['avg_error_high_vol']:.4f} error\n"
            f"Improvement:       {(1 - metadata['avg_error_high_vol']/max(metadata['avg_error_low_vol'],0.0001))*100:.1f}%\n\n"
            f"Conclusion: {'VOLUME IMPROVES PRICING' if metadata['correlation'] < -0.01 else 'WEAK VOLUME EFFECT'}"
        )
        ax.text(0.05, 0.95, text, transform=ax.transAxes, fontsize=11,
                verticalalignment='top', fontfamily='monospace',
                bbox=dict(boxstyle='round,pad=0.8', facecolor='#1a1a26', edgecolor='#6c5ce7', alpha=0.95, linewidth=1.5))

        plt.suptitle("Price Impact by Market Size — Position Sizing Guide", fontsize=14, fontweight='bold')
        plt.tight_layout()
        return fig
