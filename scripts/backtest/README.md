# Weather Backtest Scripts (2026-03-20)

Scripts used for the weather calibration overhaul. All run from the polyclawd project root on VPS.

## Usage

```bash
cd /var/www/virtuosocrypto.com/polyclawd
PYTHONPATH=. venv/bin/python3 scripts/backtest/<script>.py
```

## Scripts

| Script | Purpose | Re-run when... |
|--------|---------|----------------|
| `build_calibrator.py` | Builds isotonic calibration curve from resolved forecasts | Monthly, or after 100+ new resolutions |
| `backtest_calibration.py` | Replays OLD vs NEW model on all resolved forecasts | After changing calibration, weights, or thresholds |
| `backfill_source_rmse.py` | Fills actual temps into source_city_rmse, computes per-source accuracy | After new forecast_log actuals are backfilled |
| `calibration_curve.py` | Raw calibration analysis: predicted vs actual by bucket | When investigating calibration drift |
| `weather_deep.py` | Full diagnostic: per-city errors, source health, paper positions | General health check |
| `source_diff.py` | Compares source forecasts side by side | When evaluating source quality |

## Vault Docs
- Full writeup: `WEATHER_CALIBRATION_OVERHAUL.md`
- Backtest results: `BACKTEST_RESULTS_2026-03-20.md`
