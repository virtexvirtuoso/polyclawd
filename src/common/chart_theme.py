"""Virtuoso dark chart theme — matches the Polyclawd dashboard."""

import matplotlib.pyplot as plt
import matplotlib as mpl
from matplotlib import patheffects
import numpy as np


COLORS = {
    'bg': '#0a0a0f',
    'surface': '#12121a',
    'surface2': '#1a1a26',
    'border': '#2a2a3a',
    'accent': '#6c5ce7',
    'accent2': '#a29bfe',
    'green': '#00e676',
    'red': '#ff5252',
    'orange': '#ffab40',
    'cyan': '#18ffff',
    'text': '#e8e8f0',
    'text2': '#8888a0',
    'gold': '#ffd700',
    'teal': '#1abc9c',
    'pink': '#fd79a8',
    'blue': '#74b9ff',
}

PALETTE = ['#6c5ce7', '#00e676', '#18ffff', '#ffab40', '#ff5252', '#a29bfe', '#ffd700', '#1abc9c', '#fd79a8', '#74b9ff']

# For bar charts where you want weekend/weekday distinction
WEEKEND_COLOR = '#6c5ce7'
WEEKDAY_COLOR = '#18ffff'
BAR_COLORS = PALETTE

# Gradient-like pairs for dual series
SERIES_A = '#6c5ce7'
SERIES_B = '#00e676'


def apply_theme():
    """Apply the Virtuoso dark theme globally."""
    plt.rcParams.update({
        # Figure
        'figure.facecolor': COLORS['bg'],
        'figure.edgecolor': COLORS['bg'],
        'figure.dpi': 150,

        # Axes
        'axes.facecolor': COLORS['surface'],
        'axes.edgecolor': COLORS['border'],
        'axes.labelcolor': COLORS['text2'],
        'axes.titlecolor': COLORS['text'],
        'axes.titlesize': 14,
        'axes.titleweight': 'bold',
        'axes.labelsize': 10,
        'axes.grid': True,
        'axes.prop_cycle': mpl.cycler(color=PALETTE),
        'axes.spines.top': False,
        'axes.spines.right': False,

        # Grid
        'grid.color': COLORS['border'],
        'grid.alpha': 0.25,
        'grid.linewidth': 0.4,
        'grid.linestyle': '--',

        # Ticks
        'xtick.color': COLORS['text2'],
        'ytick.color': COLORS['text2'],
        'xtick.labelsize': 9,
        'ytick.labelsize': 9,
        'xtick.direction': 'out',
        'ytick.direction': 'out',

        # Text
        'text.color': COLORS['text'],
        'font.family': 'sans-serif',
        'font.sans-serif': ['Helvetica Neue', 'Helvetica', 'Arial', 'DejaVu Sans'],
        'font.size': 10,

        # Legend
        'legend.facecolor': COLORS['surface2'],
        'legend.edgecolor': COLORS['border'],
        'legend.fontsize': 9,
        'legend.labelcolor': COLORS['text'],
        'legend.framealpha': 0.9,

        # Savefig
        'savefig.facecolor': COLORS['bg'],
        'savefig.edgecolor': COLORS['bg'],
        'savefig.bbox': 'tight',
        'savefig.pad_inches': 0.3,

        # Lines
        'lines.linewidth': 2.0,
        'lines.antialiased': True,
    })


def style_ax(ax, title=None):
    """Apply consistent styling to an axes object."""
    if title:
        ax.set_title(title, pad=12, fontsize=13, fontweight='bold', color=COLORS['text'])
    ax.spines['left'].set_color(COLORS['border'])
    ax.spines['bottom'].set_color(COLORS['border'])
    ax.tick_params(colors=COLORS['text2'], length=4)


def summary_box(ax, text, loc='lower right', fontsize=9):
    """Add a styled summary text box that matches the dashboard theme."""
    props = dict(
        boxstyle='round,pad=0.8',
        facecolor=COLORS['surface2'],
        edgecolor=COLORS['accent'],
        alpha=0.95,
        linewidth=1.5,
    )
    anchors = {
        'lower right': (0.97, 0.03, 'right', 'bottom'),
        'upper right': (0.97, 0.97, 'right', 'top'),
        'lower left': (0.03, 0.03, 'left', 'bottom'),
        'upper left': (0.03, 0.97, 'left', 'top'),
        'center right': (0.97, 0.5, 'right', 'center'),
    }
    x, y, ha, va = anchors.get(loc, (0.97, 0.03, 'right', 'bottom'))
    ax.text(x, y, text, transform=ax.transAxes,
            fontsize=fontsize, fontfamily='monospace',
            verticalalignment=va, horizontalalignment=ha,
            bbox=props, color=COLORS['text'])


def gradient_bar(ax, x, heights, width=0.6, color=None):
    """Draw bars with slight gradient effect using layered alpha."""
    color = color or COLORS['accent']
    bars = ax.bar(x, heights, width=width, color=color, alpha=0.85,
                  edgecolor=color, linewidth=0.5, zorder=3)
    # Add glow effect at top
    for bar in bars:
        bar.set_path_effects([
            patheffects.withStroke(linewidth=1, foreground=color + '40')
        ])
    return bars
