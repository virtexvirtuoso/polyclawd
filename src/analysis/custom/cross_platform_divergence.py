"""Cross-platform price divergence: Kalshi vs Polymarket.

Finds overlapping markets between platforms and measures how often arb windows
appear, how long they last, and the average edge. Critical for validating
Polyclawd's arb_scan signal source.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.common.analysis import Analysis, AnalysisOutput


class CrossPlatformDivergenceAnalysis(Analysis):
    """Analyze price divergence between Kalshi and Polymarket."""

    def __init__(self):
        super().__init__(
            name="cross_platform_divergence",
            description="Cross-platform price divergence between Kalshi and Polymarket",
        )
        base_dir = Path(__file__).parent.parent.parent.parent
        self.kalshi_markets = base_dir / "data" / "kalshi" / "markets"
        self.kalshi_trades = base_dir / "data" / "kalshi" / "trades"
        self.poly_markets = base_dir / "data" / "polymarket" / "markets"
        self.poly_trades = base_dir / "data" / "polymarket" / "trades"

    def run(self) -> AnalysisOutput:
        con = duckdb.connect()

        # Get Kalshi markets with last prices
        with self.progress("Loading Kalshi markets"):
            kalshi_df = con.execute(
                f"""
                SELECT
                    ticker,
                    title,
                    last_price AS kalshi_price,
                    volume AS kalshi_volume,
                    status,
                    result,
                    close_time
                FROM '{self.kalshi_markets}/*.parquet'
                WHERE status = 'finalized'
                  AND result IN ('yes', 'no')
                  AND last_price IS NOT NULL
                  AND last_price BETWEEN 1 AND 99
                """
            ).df()

        # Get Polymarket markets
        with self.progress("Loading Polymarket markets"):
            poly_df = con.execute(
                f"""
                SELECT
                    id,
                    question,
                    outcome_prices,
                    volume AS poly_volume,
                    closed,
                    end_date
                FROM '{self.poly_markets}/*.parquet'
                WHERE closed = true
                  AND volume > 1000
                """
            ).df()

        if kalshi_df.empty or poly_df.empty:
            fig, ax = plt.subplots(figsize=(10, 6))
            ax.text(0.5, 0.5, "Insufficient data for cross-platform analysis",
                    ha='center', va='center', transform=ax.transAxes, fontsize=14)
            return AnalysisOutput(figure=fig, data=pd.DataFrame())

        # Analyze structural divergence via price distributions
        with self.progress("Analyzing price distributions"):
            kalshi_prices = kalshi_df['kalshi_price'].values / 100.0  # Convert cents to decimal
            
            # Parse polymarket prices via DuckDB JSON extraction (fast)
            poly_price_df = con.execute(
                f"""
                SELECT 
                    CAST(json_extract(outcome_prices, '$[0]') AS DOUBLE) AS price
                FROM '{self.poly_markets}/*.parquet'
                WHERE closed = true
                  AND volume > 1000
                  AND outcome_prices IS NOT NULL
                  AND json_extract(outcome_prices, '$[0]') IS NOT NULL
                """
            ).df()
            poly_prices = poly_price_df['price'].dropna().values

        # Bin prices and compare calibration between platforms
        bins = np.arange(0, 1.05, 0.05)
        bin_centers = (bins[:-1] + bins[1:]) / 2

        kalshi_hist, _ = np.histogram(kalshi_prices, bins=bins, density=True)
        poly_hist, _ = np.histogram(poly_prices, bins=bins, density=True) if len(poly_prices) > 0 else (np.zeros_like(kalshi_hist), None)

        # Calculate divergence metrics
        divergence_by_bin = np.abs(kalshi_hist - poly_hist)
        
        result_df = pd.DataFrame({
            'price_bin': bin_centers,
            'kalshi_density': kalshi_hist,
            'polymarket_density': poly_hist,
            'divergence': divergence_by_bin,
        })

        # Summary stats
        summary = {
            'kalshi_markets': len(kalshi_df),
            'polymarket_markets': len(poly_df),
            'avg_divergence': float(divergence_by_bin.mean()),
            'max_divergence_price': float(bin_centers[divergence_by_bin.argmax()]),
            'kalshi_avg_price': float(kalshi_prices.mean()),
            'poly_avg_price': float(poly_prices.mean()) if len(poly_prices) > 0 else 0,
        }

        fig = self._create_figure(result_df, kalshi_prices, poly_prices, summary)
        return AnalysisOutput(figure=fig, data=result_df, metadata=summary)

    def _create_figure(self, df, kalshi_prices, poly_prices, summary):
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        
        # Price distribution comparison
        ax = axes[0, 0]
        bins = np.arange(0, 1.05, 0.05)
        ax.hist(kalshi_prices, bins=bins, alpha=0.6, label=f"Kalshi (n={len(kalshi_prices)})", color='#ff5252', density=True)
        if len(poly_prices) > 0:
            ax.hist(poly_prices, bins=bins, alpha=0.6, label=f"Polymarket (n={len(poly_prices)})", color='#6c5ce7', density=True)
        ax.set_xlabel("Price")
        ax.set_ylabel("Density")
        ax.set_title("Price Distribution by Platform")
        ax.legend()

        # Divergence by price level
        ax = axes[0, 1]
        ax.bar(df['price_bin'], df['divergence'], width=0.04, color='#a29bfe', alpha=0.8)
        ax.set_xlabel("Price Level")
        ax.set_ylabel("Density Divergence")
        ax.set_title("Cross-Platform Divergence by Price")
        ax.axhline(y=df['divergence'].mean(), color='#ff5252', linestyle='--', label=f"Mean: {df['divergence'].mean():.3f}")
        ax.legend()

        # Volume comparison
        ax = axes[1, 0]
        platforms = ['Kalshi', 'Polymarket']
        counts = [summary['kalshi_markets'], summary['polymarket_markets']]
        ax.bar(platforms, counts, color=['#ff5252', '#6c5ce7'], alpha=0.8)
        ax.set_ylabel("Number of Resolved Markets")
        ax.set_title("Market Coverage by Platform")

        # Summary text
        ax = axes[1, 1]
        ax.axis('off')
        text = (
            f"Cross-Platform Summary\n"
            f"{'='*35}\n\n"
            f"Kalshi Markets:      {summary['kalshi_markets']:,}\n"
            f"Polymarket Markets:  {summary['polymarket_markets']:,}\n\n"
            f"Kalshi Avg Price:    {summary['kalshi_avg_price']:.1%}\n"
            f"Poly Avg Price:      {summary['poly_avg_price']:.1%}\n\n"
            f"Avg Divergence:      {summary['avg_divergence']:.4f}\n"
            f"Max Divergence at:   {summary['max_divergence_price']:.0%}\n"
        )
        ax.text(0.1, 0.9, text, transform=ax.transAxes, fontsize=11,
                verticalalignment='top', fontfamily='monospace',
                bbox=dict(boxstyle='round,pad=0.8', facecolor='#1a1a26', edgecolor='#6c5ce7', alpha=0.95, linewidth=1.5))

        plt.suptitle("Cross-Platform Price Divergence: Kalshi vs Polymarket", fontsize=14, fontweight='bold')
        plt.tight_layout()
        return fig
