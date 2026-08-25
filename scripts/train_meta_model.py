#!/usr/bin/env python3
"""
Meta-labeling model: predict P(profit | signal features).
Trains on archived + current resolved trades.
Uses logistic regression (simple, interpretable, works with small data).
"""
import sqlite3
import json
import pickle
import os
import numpy as np
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"
MODEL_PATH = Path(__file__).parent.parent / "storage" / "meta_model.pkl"
STATS_PATH = Path(__file__).parent.parent / "storage" / "meta_model_stats.json"

# Archetype encoding
ARCHETYPES = [
    "weather", "entertainment", "geopolitical", "election", "price_above",
    "sports_winner", "sports_single_game", "social_count", "deadline_binary",
    "ai_model", "other"
]

def archetype_to_idx(arch):
    arch = (arch or "other").lower()
    for i, a in enumerate(ARCHETYPES):
        if a in arch:
            return i
    return len(ARCHETYPES) - 1  # "other"

def extract_features(row):
    """Extract feature vector from a trade row."""
    entry = row["entry_price"] or 0.5
    conf = row["confidence"] or 0.5
    edge = row["edge_pct"] or 0
    side = row["side"] or "NO"
    arch = row["archetype"] or "other"
    
    # Market agreement (disagreement)
    if side == "YES":
        disagreement = abs(conf - entry)
    else:
        disagreement = abs(conf - (1 - entry))
    
    # Effective price (what we paid)
    eff_price = entry if side == "YES" else (1 - entry)
    
    # Potential return ratio
    potential_return = (1 / eff_price - 1) if eff_price > 0 else 0
    
    # Archetype one-hot (simplified to index for small model)
    arch_idx = archetype_to_idx(arch)
    
    # Side encoding
    side_num = 1.0 if side == "YES" else 0.0
    
    features = [
        eff_price,           # what we paid
        edge,                # edge at entry
        conf,                # model confidence
        disagreement,        # model-market disagreement
        potential_return,    # risk/reward ratio
        side_num,            # YES=1, NO=0
        arch_idx / len(ARCHETYPES),  # normalized archetype
    ]
    return features

def load_trades():
    """Load all resolved trades from archive + current, with timestamps for recency weighting."""
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.execute("PRAGMA busy_timeout=8000")
    conn.row_factory = sqlite3.Row
    
    trades = []
    
    # Archived trades
    try:
        rows = conn.execute("""
            SELECT side, entry_price, confidence, edge_pct, pnl, archetype, strategy, closed_at
            FROM paper_positions_archive
            WHERE pnl IS NOT NULL
        """).fetchall()
        trades.extend(rows)
    except Exception:
        pass
    
    # Current resolved trades
    try:
        rows = conn.execute("""
            SELECT side, entry_price, confidence, edge_pct, pnl, archetype, strategy, closed_at
            FROM paper_positions
            WHERE status != 'open' AND pnl IS NOT NULL
        """).fetchall()
        trades.extend(rows)
    except Exception:
        pass
    
    conn.close()
    return trades

def train():
    """Train meta-labeling model."""
    trades = load_trades()
    if len(trades) < 20:
        print(f"Only {len(trades)} trades — need at least 20. Skipping.")
        return False
    
    X = []
    y = []
    sample_weights = []
    
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    
    for t in trades:
        features = extract_features(t)
        label = 1 if (t["pnl"] or 0) > 0 else 0
        X.append(features)
        y.append(label)
        
        # Recency weighting: recent trades weighted 3-5x more than old ones
        # Half-life of 14 days: trade from 14 days ago = 50% weight, 28 days = 25%, etc.
        # Minimum weight = 0.2 (old trades still contribute, just less)
        HALF_LIFE_DAYS = 14.0
        try:
            closed_str = t["closed_at"] or ""
            if closed_str:
                closed_dt = datetime.fromisoformat(closed_str.replace("Z", "+00:00"))
                if closed_dt.tzinfo is None:
                    closed_dt = closed_dt.replace(tzinfo=timezone.utc)
                days_ago = (now - closed_dt).total_seconds() / 86400
            else:
                days_ago = 60  # assume old if no timestamp
        except Exception:
            days_ago = 60
        
        weight = max(0.2, 0.5 ** (days_ago / HALF_LIFE_DAYS))
        sample_weights.append(weight)
    
    X = np.array(X)
    y = np.array(y)
    w = np.array(sample_weights)
    w = w / w.sum() * len(w)  # normalize so total weight = n_samples
    
    # Standardize features
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std == 0] = 1  # avoid div by zero
    X_norm = (X - mean) / std
    
    # Weighted logistic regression via numpy (no sklearn dependency)
    # Gradient descent with sample weights
    n_features = X_norm.shape[1]
    weights = np.zeros(n_features)
    bias = 0.0
    lr = 0.1
    
    for epoch in range(1000):
        z = X_norm @ weights + bias
        pred = 1 / (1 + np.exp(-np.clip(z, -500, 500)))
        
        # Weighted binary cross-entropy gradient
        error = (pred - y) * w
        weights -= lr * (X_norm.T @ error) / len(y)
        bias -= lr * error.mean()
    
    # Evaluate
    final_pred = 1 / (1 + np.exp(-np.clip(X_norm @ weights + bias, -500, 500)))
    predicted_labels = (final_pred >= 0.5).astype(int)
    accuracy = (predicted_labels == y).mean()
    
    # Stats
    n_total = len(y)
    n_wins = int(y.sum())
    n_losses = n_total - n_wins
    base_rate = n_wins / n_total
    
    # Feature importance (absolute weight magnitude)
    feature_names = ["eff_price", "edge", "confidence", "disagreement", "potential_return", "side", "archetype"]
    importance = sorted(zip(feature_names, np.abs(weights).tolist()), key=lambda x: -x[1])
    
    # Save model
    model = {
        "weights": weights.tolist(),
        "bias": float(bias),
        "mean": mean.tolist(),
        "std": std.tolist(),
        "feature_names": feature_names,
    }
    
    with open(str(MODEL_PATH), "wb") as f:
        pickle.dump(model, f)
    
    # Weighted win rate (what the model "sees" as the base rate)
    weighted_wins = float((y * w).sum())
    weighted_total = float(w.sum())
    weighted_base_rate = weighted_wins / weighted_total if weighted_total > 0 else base_rate
    
    stats = {
        "n_trades": n_total,
        "n_wins": n_wins,
        "n_losses": n_losses,
        "base_win_rate": round(base_rate, 3),
        "weighted_base_rate": round(weighted_base_rate, 3),
        "model_accuracy": round(float(accuracy), 3),
        "improvement": round(float(accuracy) - base_rate, 3),
        "feature_importance": importance,
        "half_life_days": 14,
        "min_weight": 0.2,
        "trained_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
    
    with open(str(STATS_PATH), "w") as f:
        json.dump(stats, f, indent=2)
    
    print(f"Trained on {n_total} trades ({n_wins}W/{n_losses}L)")
    print(f"Base win rate: {base_rate:.1%}")
    print(f"Model accuracy: {accuracy:.1%} (+{accuracy - base_rate:.1%})")
    print(f"Top features: {importance[:3]}")
    print(f"Saved to {MODEL_PATH}")
    
    return True

if __name__ == "__main__":
    train()
