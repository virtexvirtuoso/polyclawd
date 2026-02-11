"""Category edge persistence — which market categories have consistent mispricing?

Uses Kalshi event_ticker categories and Polymarket slugs to group markets, then
checks if certain categories show persistent pricing errors.
"""

from __future__ import annotations
from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.common.analysis import Analysis, AnalysisOutput


class CategoryEdgePersistenceAnalysis(Analysis):
    """Analyze pricing edge persistence by market category."""

    def __init__(self):
        super().__init__(
            name="category_edge_persistence",
            description="Edge persistence by market category",
        )
        base_dir = Path(__file__).parent.parent.parent.parent
        self.kalshi_markets = base_dir / "data" / "kalshi" / "markets"
        self.poly_markets = base_dir / "data" / "polymarket" / "markets"

    def run(self) -> AnalysisOutput:
        con = duckdb.connect()

        with self.progress("Loading Kalshi markets with categories"):
            kalshi = con.execute(f"""
                SELECT
                    event_ticker,
                    REGEXP_EXTRACT(event_ticker, '^([A-Z]+)', 1) AS category,
                    last_price/100.0 AS price,
                    CASE WHEN result='yes' THEN 1.0 ELSE 0.0 END AS resolved_yes,
                    volume
                FROM '{self.kalshi_markets}/*.parquet'
                WHERE status='finalized' AND result IN ('yes','no')
                  AND last_price BETWEEN 1 AND 99 AND volume > 0
                  AND event_ticker IS NOT NULL
            """).df()

        if kalshi.empty:
            fig, ax = plt.subplots()
            ax.text(0.5, 0.5, "No data", ha='center', va='center', transform=ax.transAxes)
            return AnalysisOutput(figure=fig, data=pd.DataFrame())

        with self.progress("Computing category-level edge"):
            kalshi['pred_error'] = np.abs(kalshi['price'] - kalshi['resolved_yes'])
            kalshi['correct'] = (
                ((kalshi['price'] > 0.5) & (kalshi['resolved_yes'] == 1)) |
                ((kalshi['price'] <= 0.5) & (kalshi['resolved_yes'] == 0))
            ).astype(int)

            # Calibration error: for bins of price, how far is actual resolution from price?
            kalshi['price_bin'] = (kalshi['price'] * 10).round() / 10  # Round to nearest 0.1

            cat_stats = kalshi.groupby('category').agg(
                markets=('volume', 'count'),
                avg_error=('pred_error', 'mean'),
                accuracy=('correct', lambda x: 100 * x.mean()),
                total_volume=('volume', 'sum'),
                avg_price=('price', 'mean'),
            ).reset_index()

            # Only keep categories with enough data
            cat_stats = cat_stats[cat_stats['markets'] >= 20].sort_values('avg_error', ascending=False)

            # Top mispriced and best calibrated
            top_mispriced = cat_stats.head(15)
            best_calibrated = cat_stats.tail(15).sort_values('avg_error')

        # Calibration by price bin across all categories
        with self.progress("Building calibration curves"):
            cal = kalshi.groupby('price_bin').agg(
                actual_yes=('resolved_yes', 'mean'),
                count=('volume', 'count'),
            ).reset_index()
            cal = cal[cal['count'] >= 50]

        metadata = {
            'total_categories': len(cat_stats),
            'total_markets': int(cat_stats['markets'].sum()),
            'worst_category': cat_stats.iloc[0]['category'] if not cat_stats.empty else 'N/A',
            'worst_error': float(cat_stats.iloc[0]['avg_error']) if not cat_stats.empty else 0,
            'best_category': cat_stats.iloc[-1]['category'] if not cat_stats.empty else 'N/A',
            'best_error': float(cat_stats.iloc[-1]['avg_error']) if not cat_stats.empty else 0,
        }

        fig = self._create_figure(top_mispriced, best_calibrated, cal, metadata)
        return AnalysisOutput(figure=fig, data=cat_stats, metadata=metadata)

    def _create_figure(self, worst, best, cal, metadata):
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))

        ax = axes[0, 0]
        ax.barh(range(len(worst)), worst['avg_error'], color='#ff5252', alpha=0.8)
        ax.set_yticks(range(len(worst)))
        ax.set_yticklabels(worst['category'], fontsize=8)
        ax.set_xlabel("Avg Prediction Error")
        ax.set_title("Most Mispriced Categories (Top 15)")
        ax.invert_yaxis()

        ax = axes[0, 1]
        ax.barh(range(len(best)), best['avg_error'], color='#00e676', alpha=0.8)
        ax.set_yticks(range(len(best)))
        ax.set_yticklabels(best['category'], fontsize=8)
        ax.set_xlabel("Avg Prediction Error")
        ax.set_title("Best Calibrated Categories (Top 15)")
        ax.invert_yaxis()

        ax = axes[1, 0]
        if not cal.empty:
            ax.scatter(cal['price_bin'], cal['actual_yes'], s=cal['count'].clip(upper=500)/5,
                      color='#6c5ce7', alpha=0.7)
            ax.plot([0, 1], [0, 1], 'k--', alpha=0.3, label='Perfect')
            ax.set_xlabel("Predicted Price")
            ax.set_ylabel("Actual Resolution Rate")
            ax.set_title("Kalshi Calibration Curve")
            ax.legend()

        ax = axes[1, 1]
        ax.axis('off')
        text = (
            f"Category Edge Summary\n{'='*35}\n\n"
            f"Categories (≥20 mkts): {metadata['total_categories']}\n"
            f"Total markets:         {metadata['total_markets']:,}\n\n"
            f"Most mispriced:  {metadata['worst_category']}\n"
            f"  Error: {metadata['worst_error']:.4f}\n\n"
            f"Best calibrated: {metadata['best_category']}\n"
            f"  Error: {metadata['best_error']:.4f}\n"
        )
        ax.text(0.05, 0.95, text, transform=ax.transAxes, fontsize=11,
                verticalalignment='top', fontfamily='monospace',
                bbox=dict(boxstyle='round,pad=0.8', facecolor='#1a1a26', edgecolor='#6c5ce7', alpha=0.95, linewidth=1.5))

        plt.suptitle("Category Edge Persistence — Kalshi Market Analysis", fontsize=14, fontweight='bold')
        plt.tight_layout()
        return fig
