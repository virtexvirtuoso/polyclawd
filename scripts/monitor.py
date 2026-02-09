#!/usr/bin/env python3
"""
Polyclawd Signal Monitor with Ollama LLM Analysis
Monitors for high-confidence signals and uses local Ollama for analysis.
Executes approved trades automatically.

Usage:
    python3 monitor.py

Runs as daemon, polls every 30 seconds.
"""

import json
import time
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

# Import OpenClaw alerting
try:
    from openclaw_alerts import alert_openclaw, format_signal_alert, alert_high_edge_signal
    ALERTS_ENABLED = True
except ImportError:
    ALERTS_ENABLED = False

# Configuration
POLYCLAWD_API = "http://localhost:8420"
MIN_EDGE_ALERT = 5.0  # Alert via OpenClaw when edge >= this
OLLAMA_API = "http://localhost:11434"
POLL_INTERVAL = 30  # seconds
MIN_CONFIDENCE = 45  # Only analyze signals >= this confidence
OLLAMA_MODEL = "llama3.2:3b"
DATA_DIR = Path(__file__).parent.parent / "data"

# Track what we've already analyzed
analyzed_signals = set()


def log(msg: str):
    """Log with timestamp"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def api_get(endpoint: str, base_url: str = POLYCLAWD_API) -> dict:
    """Make GET request"""
    try:
        url = f"{base_url}{endpoint}"
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        log(f"API error: {e}")
        return {}


def api_post(endpoint: str, data: dict = None, base_url: str = POLYCLAWD_API) -> dict:
    """Make POST request"""
    try:
        url = f"{base_url}{endpoint}"
        req_data = json.dumps(data).encode() if data else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(url, data=req_data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        log(f"API error: {e}")
        return {}


def ollama_analyze(signal: dict) -> dict:
    """
    Use Ollama to analyze a trading signal.
    Returns: {"action": "TRADE" or "SKIP", "reason": "..."}
    """
    prompt = f"""You are a prediction market trading analyst. Analyze this signal and decide TRADE or SKIP.

SIGNAL:
- Market: {signal.get('market', '')[:100]}
- Side: {signal.get('side', '')}
- Confidence: {signal.get('confidence', 0):.1f}/100
- Source: {signal.get('source', '')}
- Reasoning: {signal.get('reasoning', '')[:100]}

DECISION CRITERIA:
1. Is this a quality market? (Skip weather, obscure sports, ambiguous resolution)
2. Is the confidence high enough? (>50 is good, >70 is strong)
3. Does the reasoning make sense?
4. Is the timing appropriate? (Not too close to resolution)

Respond with ONLY a JSON object, no other text:
{{"action": "TRADE" or "SKIP", "reason": "one sentence explanation"}}"""

    try:
        response = api_post("/api/generate", {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3}
        }, base_url=OLLAMA_API)
        
        if not response:
            return {"action": "SKIP", "reason": "Ollama unavailable"}
        
        text = response.get("response", "")
        
        # Try to parse JSON from response
        try:
            # Find JSON in response
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(text[start:end])
                return result
        except:
            pass
        
        # Fallback: look for TRADE or SKIP keyword
        if "TRADE" in text.upper():
            return {"action": "TRADE", "reason": "LLM approved"}
        else:
            return {"action": "SKIP", "reason": text[:100] if text else "No clear decision"}
            
    except Exception as e:
        log(f"Ollama error: {e}")
        return {"action": "SKIP", "reason": f"Analysis failed: {str(e)[:50]}"}


def get_tradeable_signals() -> list:
    """Fetch signals that pass basic filters"""
    signals_data = api_get("/api/signals")
    if not signals_data:
        return []
    
    tradeable = []
    garbage_keywords = ["temperature", "weather"]
    
    for sig in signals_data.get("actionable_signals", []):
        conf = sig.get("confidence", 0)
        market = sig.get("market", "").lower()
        market_id = sig.get("market_id") or sig.get("market", "")[:30]
        
        # Basic filters
        if conf < MIN_CONFIDENCE:
            continue
        if any(kw in market for kw in garbage_keywords):
            continue
        
        # Create unique key
        sig_key = f"{market_id}:{sig.get('side', '')}"
        
        # Skip if already analyzed this session
        if sig_key in analyzed_signals:
            continue
        
        tradeable.append({
            "key": sig_key,
            "market_id": market_id,
            "market": sig.get("market", "")[:100],
            "platform": sig.get("platform", ""),
            "side": sig.get("side", ""),
            "confidence": conf,
            "source": sig.get("source", ""),
            "reasoning": sig.get("reasoning", "")[:150],
            "price": sig.get("price", 0.5)
        })
    
    return tradeable


def save_analysis_log(signal: dict, decision: dict):
    """Save analysis to log file"""
    log_file = DATA_DIR / "ollama_analysis.jsonl"
    entry = {
        "timestamp": datetime.now().isoformat(),
        "signal": signal,
        "decision": decision
    }
    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except:
        pass


def main():
    """Main monitor loop"""
    log("=" * 60)
    log("Polyclawd Signal Monitor + Ollama LLM")
    log(f"Model: {OLLAMA_MODEL} | Poll: {POLL_INTERVAL}s | Min conf: {MIN_CONFIDENCE}")
    log(f"OpenClaw Alerts: {'✅ Enabled' if ALERTS_ENABLED else '❌ Disabled'} | Min edge: {MIN_EDGE_ALERT}%")
    log("=" * 60)
    
    # Check Ollama is running
    test = api_get("/api/tags", base_url=OLLAMA_API)
    if not test:
        log("⚠️  Ollama not responding! Starting anyway...")
    else:
        models = [m.get("name", "") for m in test.get("models", [])]
        log(f"✅ Ollama ready with models: {', '.join(models[:3])}")
    
    trade_count = 0
    skip_count = 0
    
    while True:
        try:
            # Look for new high-confidence signals
            signals = get_tradeable_signals()
            
            for sig in signals:
                log(f"🔍 Analyzing: [{sig['confidence']:.0f}] {sig['platform'].upper()} {sig['side']} - {sig['market'][:40]}")
                
                # Get Ollama decision
                decision = ollama_analyze(sig)
                action = decision.get("action", "SKIP").upper()
                reason = decision.get("reason", "")
                
                # Mark as analyzed
                analyzed_signals.add(sig["key"])
                
                # Save to log
                save_analysis_log(sig, decision)
                
                if action == "TRADE":
                    log(f"   ✅ TRADE: {reason}")
                    
                    # Send alert via OpenClaw for high-edge signals
                    if ALERTS_ENABLED and sig.get("confidence", 0) >= 60:
                        edge_pct = (sig["confidence"] - 50) * 0.2  # Rough edge estimate
                        if edge_pct >= MIN_EDGE_ALERT:
                            alert_msg = format_signal_alert(
                                market=sig["market"],
                                side=sig["side"],
                                price=sig.get("price", 0.5),
                                edge=edge_pct,
                                confidence=sig["confidence"],
                                source=sig.get("source")
                            )
                            if alert_openclaw(alert_msg):
                                log(f"   📤 Alert sent to OpenClaw")
                    
                    # Execute the trade
                    result = api_post("/api/engine/trigger")
                    
                    if result.get("action") == "traded":
                        trade_count += 1
                        log(f"   🚀 EXECUTED! Total trades: {trade_count}")
                    else:
                        log(f"   ⚠️  Engine: {result.get('action', 'unknown')} - {result.get('reason', '')}")
                
                else:
                    skip_count += 1
                    log(f"   ⏭️  SKIP: {reason}")
                
                # Small delay between analyses
                time.sleep(2)
            
            # Status every 5 minutes
            if int(time.time()) % 300 < POLL_INTERVAL:
                log(f"📊 Status: {trade_count} trades, {skip_count} skips, {len(analyzed_signals)} analyzed")
            
        except KeyboardInterrupt:
            log("Shutting down...")
            log(f"Final: {trade_count} trades, {skip_count} skips")
            break
        except Exception as e:
            log(f"Error: {e}")
        
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
