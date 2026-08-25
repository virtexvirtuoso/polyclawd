#!/usr/bin/env python3
"""
poly_ws.py — Phase 3 Polymarket CLOB `market` WebSocket client.

P3.0: read-only spike (validated live 2026-06-02).
P3.1: publish live book/trade/status to memcached (coalesced) + wire source_health.
      Still standalone-runnable (`--seconds N`); becomes polyclawd-ws.service in P3.2.

Design: 02-Projects/Polyclawd/Development/Phase3-WebSocket-Design-2026-06-02.md
Reader side: api/services/poly_ws_reader.py (async, fallback-safe).

Run:  python services/poly_ws.py --seconds 45            (publish on if memcached up)
      python services/poly_ws.py --seconds 45 --no-publish
"""
import argparse
import asyncio
import json
import os
import random
import time
from collections import defaultdict, deque

try:
    import websockets
except ImportError:  # pragma: no cover
    raise SystemExit("websockets not installed")

try:
    import aiomcache
except ImportError:  # pragma: no cover
    aiomcache = None

try:  # optional — only present when run from the app root
    from api.services import source_health as _sh
except Exception:  # pragma: no cover
    _sh = None

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MC_HOST, MC_PORT = "localhost", 11211
BOOK_TTL = 15            # exptime: a dead WS auto-expires -> readers see staleness

# Fast-move detection (Phase 3 latency fix, 2026-07-10): a >=10pp mid move
# within 60s usually means sharp flow repricing a live-game event before the
# scoreboard catches up. Events published to poly:moves:recent; the scheduler
# burst loop consumes them and fires the live monitors immediately.
MOVE_PP = 0.10           # mid move threshold (absolute probability)
MOVE_WINDOW = 60         # s — lookback for the move
MOVE_COOLDOWN = 180      # s — min gap between events per token
MOVE_WARMUP = 90         # s — suppress move events after (re)connect and per-token
                         #     first sight: first snapshot vs stale prior is not a move
MOVES_TTL = 300          # s — poly:moves:recent expiry
PUBLISH_INTERVAL = 0.4   # coalescing flush cadence (latest-wins per token)
STATUS_INTERVAL = 5.0
SOURCE = "polymarket_ws"
TOP_LEVELS = 10
WS_HTTP_PORT = int(os.environ.get("POLY_WS_PORT", "8423"))

DEFAULT_TOKENS = [
    "36745488872946086676485868481750520284432732072201088040141716125862310182937",  # LoL BO5
    "84033004766153735786060235317843648243853316932231482552279219835164780666220",  # Roland Garros
    "25714007960293389110960044475283546872601238755063051359394740854408462452120",  # MicroStrategy
    "15995243820336177841554632127584222169952027258346817014909996961549144471612",  # US x Iran
]

MAX_TOKENS = int(os.environ.get("POLY_WS_MAX_TOKENS", "200"))
UNIVERSE_INTERVAL = 60   # s — refresh desired token set from Gamma
SHED_INTERVAL = 600      # s — min gap between clean reconnects to drop resolved subs


def _fetch_universe_sync(limit=MAX_TOKENS):
    """Top markets by 24h volume (where live books matter most). Sync -> call via to_thread."""
    import urllib.request
    url = ("https://gamma-api.polymarket.com/markets?active=true&closed=false"
           f"&order=volume24hr&ascending=false&limit={limit}")
    req = urllib.request.Request(url, headers={"User-Agent": "polyclawd-ws/1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        ms = json.loads(resp.read().decode())
    toks = []
    for m in ms:
        t = m.get("clobTokenIds")
        if isinstance(t, str):
            try:
                t = json.loads(t)
            except Exception:
                t = []
        if t:
            toks.append(t[0])  # YES token
    return toks


SPORT_TAGS = [t.strip() for t in os.environ.get("POLY_WS_SPORT_TAGS", "baseball,ufc,soccer").split(",") if t.strip()]


def _fetch_sport_tags_sync(tags):
    """YES tokens for the bot's sports markets (by Gamma tag) — streamed regardless
    of 24h-volume rank so soccer/UFC/baseball edges always have a live book."""
    import urllib.request
    out = []
    for tag in tags:
        try:
            url = f"https://gamma-api.polymarket.com/events?closed=false&tag_slug={tag}&limit=100"
            req = urllib.request.Request(url, headers={"User-Agent": "polyclawd-ws/1"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                events = json.loads(resp.read().decode())
        except Exception:
            continue
        if not isinstance(events, list):
            continue
        for ev in events:
            for m in (ev.get("markets") or []):
                t = m.get("clobTokenIds")
                if isinstance(t, str):
                    try:
                        t = json.loads(t)
                    except Exception:
                        t = []
                if t:
                    out.append(t[0])
    return out

# Options close-ladder markets (NVDA/META/... weekly close) are thin and rarely rank
# in the top-volume universe, so stream them explicitly when POLY_WS_OPTIONS=1 -- bounded
# by a cap so they do not crowd out the sports/volume tokens within MAX_TOKENS.
OPTIONS_ENABLED = os.environ.get("POLY_WS_OPTIONS") == "1"
OPTIONS_TOKEN_CAP = int(os.environ.get("POLY_WS_OPTIONS_CAP", "40"))


def _fetch_options_tokens_sync(cap=OPTIONS_TOKEN_CAP):
    # YES tokens for active 'close above' stock-close ladders (the options engine's
    # universe), so executable edges there can use a live WS book. Bounded by cap.
    import urllib.request
    out = []
    try:
        url = ("https://gamma-api.polymarket.com/public-search"
               "?q=close%20above&limit_per_type=50&events_status=active")
        req = urllib.request.Request(url, headers={"User-Agent": "polyclawd-ws/1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return []
    events = data.get("events", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    for ev in events:
        for m in (ev.get("markets") or []):
            t = m.get("clobTokenIds")
            if isinstance(t, str):
                try:
                    t = json.loads(t)
                except Exception:
                    t = []
            if t:
                out.append(t[0])
            if len(out) >= cap:
                return out
    return out


class PolyWS:
    def __init__(self, tokens, publish=True):
        self.tokens = tokens
        self.publish = publish and aiomcache is not None
        self.books = {}            # token -> {bids:{p:s}, asks:{p:s}, synced, ts, hash}
        self.last_trade = {}
        self.dirty = set()         # tokens whose book changed since last publish
        self.trade_dirty = set()
        self.counts = defaultdict(int)
        self.shapes = {}
        self.reconnects = 0
        self.connected = False
        self.last_msg_ts = 0.0
        self.first_book_ts = None
        self.start_ts = time.time()
        self.mc = None
        self.published = defaultdict(int)
        self._conn_start = 0.0
        self.desired = set(tokens)       # token universe we want subscribed
        self.subscribed = set()          # tokens sent on the current connection
        self._last_shed = time.time()
        self.mid_hist = {}               # token -> deque[(ts, mid)] for move detection
        self.move_last = {}              # token -> ts of last move event
        self.move_warmup_until = 0.0     # global warm-up gate (set on each connect)
        self.tok_seen = {}               # token -> ts first observed (per-token warm-up)
        self.recent_moves = []           # rolling list published to poly:moves:recent
        self.moves_dirty = False

    # ---------- reconciliation ----------
    def _note_shape(self, et, ev):
        if et not in self.shapes:
            self.shapes[et] = sorted(ev.keys())

    def _apply(self, ev):
        et = ev.get("event_type") or ev.get("type") or "?"
        self.counts[et] += 1
        self._note_shape(et, ev)
        if et == "book":
            tok = ev.get("asset_id")
            if self.desired and tok not in self.desired:
                return
            if self.first_book_ts is None:
                self.first_book_ts = round(time.time() - self.start_ts, 2)
            b = {"bids": {}, "asks": {}, "synced": True,
                 "ts": ev.get("timestamp"), "hash": ev.get("hash")}
            for lvl in ev.get("bids", []) or []:
                try:
                    b["bids"][float(lvl["price"])] = float(lvl["size"])
                except (KeyError, ValueError, TypeError):
                    pass
            for lvl in ev.get("asks", []) or []:
                try:
                    b["asks"][float(lvl["price"])] = float(lvl["size"])
                except (KeyError, ValueError, TypeError):
                    pass
            self.books[tok] = b
            self.dirty.add(tok)
        elif et == "price_change":
            # price_changes (plural); each entry carries its OWN asset_id (P3.0 finding)
            for c in ev.get("price_changes", []) or []:
                ctok = c.get("asset_id")
                bk = self.books.get(ctok)
                if not bk:
                    continue
                side = str(c.get("side", "")).lower()
                try:
                    price = float(c.get("price", 0))
                    size = float(c.get("size", 0))
                except (ValueError, TypeError):
                    continue
                book_side = bk["bids"] if side in ("buy", "bid") else bk["asks"]
                if size == 0:
                    book_side.pop(price, None)
                else:
                    book_side[price] = size
                bk["ts"] = ev.get("timestamp")
                self.dirty.add(ctok)
        elif et == "last_trade_price":
            tok = ev.get("asset_id")
            if self.desired and tok not in self.desired:
                return
            self.last_trade[tok] = {
                "price": ev.get("price"), "size": ev.get("size"),
                "side": ev.get("side"), "ts": ev.get("timestamp"),
            }
            self.trade_dirty.add(tok)

    def _snapshot(self, tok):
        bk = self.books.get(tok)
        if not bk:
            return None
        bids = sorted(bk["bids"].items(), key=lambda x: -x[0])[:TOP_LEVELS]
        asks = sorted(bk["asks"].items(), key=lambda x: x[0])[:TOP_LEVELS]
        bb = bids[0][0] if bids else None
        ba = asks[0][0] if asks else None
        spread = round(ba - bb, 4) if (bb is not None and ba is not None) else None
        mid = round((bb + ba) / 2, 4) if (bb is not None and ba is not None) else None
        return {
            "token_id": tok, "best_bid": bb, "best_ask": ba, "spread": spread,
            "mid": mid, "bids": [[p, s] for p, s in bids], "asks": [[p, s] for p, s in asks],
            "ts": bk.get("ts"), "hash": bk.get("hash"), "synced": bk.get("synced", False),
            "pub_ts": int(time.time() * 1000),
        }

    # ---------- memcached publish ----------
    async def _mc(self):
        if self.mc is None and aiomcache is not None:
            self.mc = aiomcache.Client(MC_HOST, MC_PORT, pool_size=2)
        return self.mc

    def _check_move(self, tok, snap):
        """Detect >=MOVE_PP mid moves within MOVE_WINDOW and queue an event."""
        bb, ba = snap.get("best_bid"), snap.get("best_ask")
        if bb is None or ba is None:
            return
        try:
            mid = (float(bb) + float(ba)) / 2.0
        except (TypeError, ValueError):
            return
        now = time.time()
        hist = self.mid_hist.setdefault(tok, deque())
        hist.append((now, mid))
        while hist and now - hist[0][0] > MOVE_WINDOW:
            hist.popleft()
        old = hist[0][1]
        # Warm-up: after (re)connect or first sight of a token, the "old" mid is
        # either missing or stale — deltas are snapshot artifacts, not real moves.
        first = self.tok_seen.setdefault(tok, now)
        if now < self.move_warmup_until or now - first < MOVE_WARMUP:
            return
        if abs(mid - old) < MOVE_PP:
            return
        if now - self.move_last.get(tok, 0.0) < MOVE_COOLDOWN:
            return
        self.move_last[tok] = now
        ev = {"token_id": tok, "from": round(old, 3), "to": round(mid, 3),
              "delta": round(mid - old, 3), "ts": round(now, 1)}
        self.recent_moves = [m for m in self.recent_moves
                             if now - m["ts"] < MOVES_TTL] + [ev]
        self.moves_dirty = True
        print(f"[move] ...{tok[-10:]} {old:.2f} -> {mid:.2f} within {MOVE_WINDOW}s")

    async def _publish_loop(self):
        if not self.publish:
            return
        while True:
            await asyncio.sleep(PUBLISH_INTERVAL)
            mc = await self._mc()
            if mc is None:
                continue
            toks, self.dirty = self.dirty, set()
            ttoks, self.trade_dirty = self.trade_dirty, set()
            for tok in toks:
                snap = self._snapshot(tok)
                if snap is None:
                    continue
                self._check_move(tok, snap)
                try:
                    await mc.set(f"poly:book:{tok}".encode(),
                                 json.dumps(snap).encode(), exptime=BOOK_TTL)
                    self.published["book"] += 1
                except Exception:
                    pass
            if self.moves_dirty:
                self.moves_dirty = False
                try:
                    await mc.set(b"poly:moves:recent",
                                 json.dumps(self.recent_moves).encode(),
                                 exptime=MOVES_TTL)
                    self.published["move"] += 1
                except Exception:
                    pass
            for tok in ttoks:
                lt = self.last_trade.get(tok)
                if not lt:
                    continue
                try:
                    await mc.set(f"poly:trade:{tok}".encode(),
                                 json.dumps({**lt, "token_id": tok}).encode(), exptime=60)
                    self.published["trade"] += 1
                except Exception:
                    pass

    async def _status_loop(self):
        while True:
            await asyncio.sleep(STATUS_INTERVAL)
            status = {
                "connected": self.connected,
                "subscribed_count": len(self.subscribed),
                "last_msg_ts": int(self.last_msg_ts * 1000),
                "reconnects": self.reconnects,
                "books_live": len(self.books),
                "pub_ts": int(time.time() * 1000),
            }
            if self.publish:
                mc = await self._mc()
                if mc is not None:
                    try:
                        await mc.set(b"poly:ws:status", json.dumps(status).encode(), exptime=30)
                    except Exception:
                        pass
            # source_health (sync SQLite -> off the event loop)
            if _sh is not None:
                try:
                    if self.connected:
                        await asyncio.to_thread(_sh.touch_source, SOURCE)
                    else:
                        await asyncio.to_thread(_sh.record_failure, SOURCE, "ws disconnected")
                except Exception:
                    pass

    # ---------- ws ----------
    async def _app_ping(self, ws):
        while True:
            await asyncio.sleep(10)
            try:
                await ws.send("PING")
            except Exception:
                return

    async def run_once(self, deadline):
        async with websockets.connect(WS_URL, ping_interval=10, ping_timeout=25,
                                      max_size=2 ** 23, open_timeout=15) as ws:
            sub = sorted(self.desired)
            await ws.send(json.dumps({"assets_ids": sub, "type": "market"}))
            self.subscribed = set(sub)
            self.connected = True
            self._conn_start = time.time()
            # Reset move detection: history from before the reconnect is stale.
            self.move_warmup_until = time.time() + MOVE_WARMUP
            self.mid_hist.clear()
            print(f"[sub] {len(sub)} tokens | publish={self.publish}")
            if _sh is not None:
                try:
                    await asyncio.to_thread(_sh.record_success, SOURCE, 0.0)
                except Exception:
                    pass
            ping_task = asyncio.create_task(self._app_ping(ws))
            last_uni = time.time()
            try:
                while time.time() < deadline:
                    if time.time() - last_uni > 5:
                        last_uni = time.time()
                        if await self._apply_universe(ws):
                            break  # shed resolved markets via a clean reconnect
                    try:
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=min(5, max(0.1, deadline - time.time())))
                    except asyncio.TimeoutError:
                        continue
                    self.last_msg_ts = time.time()
                    if raw in ("PONG", "PING"):
                        self.counts["_heartbeat"] += 1
                        continue
                    try:
                        data = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    for ev in (data if isinstance(data, list) else [data]):
                        if isinstance(ev, dict):
                            self._apply(ev)
            finally:
                ping_task.cancel()
                self.connected = False

    async def run(self, seconds):
        # seconds <= 0 -> run forever (persistent service mode)
        deadline = float("inf") if seconds <= 0 else time.time() + seconds
        pub_task = asyncio.create_task(self._publish_loop())
        status_task = asyncio.create_task(self._status_loop())
        uni_task = asyncio.create_task(self._universe_loop())
        http_server = await self._start_http()
        backoff = 1.0
        try:
            while time.time() < deadline:
                try:
                    await self.run_once(deadline)
                    if time.time() >= deadline:
                        break
                    continue  # clean early return (shed) -> reconnect, no backoff
                except Exception as e:
                    self.connected = False
                    self.reconnects += 1
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    if self._conn_start and (time.time() - self._conn_start) > 30:
                        backoff = 1.0  # reset after a stable connection
                    wait = min(60.0, backoff) + random.uniform(0, 0.5)
                    print(f"[reconnect #{self.reconnects}] {type(e).__name__}: {e} -> {wait:.1f}s")
                    await asyncio.sleep(min(wait, remaining))
                    backoff = min(60.0, backoff * 2)
        finally:
            pub_task.cancel()
            status_task.cancel()
            uni_task.cancel()
            if http_server is not None:
                http_server.close()
            if self.mc is not None:
                try:
                    await self.mc.close()
                except Exception:
                    pass
        self.summary()

    async def _start_http(self):
        """Tiny status server (parity with hf_engine): GET /health|/status."""
        async def handler(reader, writer):
            try:
                await reader.readline()
                status = {
                    "connected": self.connected,
                    "subscribed_count": len(self.subscribed),
                    "books_live": len(self.books),
                    "reconnects": self.reconnects,
                    "last_msg_age_s": (round(time.time() - self.last_msg_ts, 1)
                                       if self.last_msg_ts else None),
                    "published": dict(self.published),
                }
                body = json.dumps(status).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             b"Connection: close\r\nContent-Length: " +
                             str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                except Exception:
                    pass
        try:
            srv = await asyncio.start_server(handler, "127.0.0.1", WS_HTTP_PORT)
            print(f"[http] status server on 127.0.0.1:{WS_HTTP_PORT}")
            return srv
        except Exception as e:
            print(f"[http] status server not started: {e}")
            return None

    async def _read_registered(self):
        if not self.publish:
            return []
        mc = await self._mc()
        if mc is None:
            return []
        try:
            raw = await mc.get(b"poly:ws:registered")
            return json.loads(raw) if raw else []
        except Exception:
            return []

    async def _write_watchset(self):
        if not self.publish:
            return
        mc = await self._mc()
        if mc is None:
            return
        try:
            await mc.set(b"poly:ws:watchset", json.dumps(sorted(self.desired)).encode(), exptime=180)
        except Exception:
            pass

    async def _universe_loop(self):
        """Refresh the desired token set from Gamma (top-volume) + registered hints."""
        while True:
            try:
                toks = await asyncio.to_thread(_fetch_universe_sync, MAX_TOKENS)
            except Exception as e:
                print(f"[universe] fetch failed: {type(e).__name__}: {e}")
                toks = []
            try:
                sport_toks = await asyncio.to_thread(_fetch_sport_tags_sync, SPORT_TAGS)
            except Exception:
                sport_toks = []
            if OPTIONS_ENABLED:
                try:
                    options_toks = await asyncio.to_thread(_fetch_options_tokens_sync, OPTIONS_TOKEN_CAP)
                except Exception:
                    options_toks = []
            else:
                options_toks = []
            reg = await self._read_registered()
            # Options first so they always survive the slice, then sports/registered/volume.
            # Sport-tag queries return thousands of tokens that already exceed MAX_TOKENS, so
            # options get a reserved budget ON TOP of MAX_TOKENS rather than displacing sports.
            merged = list(dict.fromkeys(
                list(self.tokens) + list(options_toks) + list(sport_toks) + list(reg) + list(toks)))
            cap = MAX_TOKENS + len(options_toks)
            self.desired = set(merged[:cap])
            await self._write_watchset()
            await asyncio.sleep(UNIVERSE_INTERVAL)

    async def _apply_universe(self, ws):
        """Apply a changed token universe. Polymarket's market channel IGNORES
        mid-stream subscribe messages (verified) — the only reliable way to change
        the subscription set is to reconnect and re-subscribe the full set. So when
        `desired` diverges from what we connected with, request a reconnect
        (rate-limited to avoid thrashing on a churning universe)."""
        new = self.desired - self.subscribed
        stale = self.subscribed - self.desired
        if (new or stale) and (time.time() - self._last_shed > 30):
            self._last_shed = time.time()
            for tok in stale:
                self.books.pop(tok, None)
                self.last_trade.pop(tok, None)
            print(f"[universe] resubscribe via reconnect: +{len(new)} -{len(stale)} "
                  f"(desired={len(self.desired)})")
            return True
        return False

    def summary(self):
        print("\n===== P3.1 SUMMARY =====")
        print("run seconds:      ", round(time.time() - self.start_ts, 1))
        print("counts by type:   ", dict(self.counts))
        print("published to mc:  ", dict(self.published), "(publish on)" if self.publish else "(publish OFF)")
        print("reconnects:       ", self.reconnects)
        for tok in self.tokens:
            snap = self._snapshot(tok)
            if snap:
                print(f"   ...{tok[-10:]} bbo={snap['best_bid']}/{snap['best_ask']} "
                      f"spread={snap['spread']} levels={len(snap['bids'])}/{len(snap['asks'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=45)
    ap.add_argument("--tokens", type=str, default=",".join(DEFAULT_TOKENS))
    ap.add_argument("--no-publish", action="store_true")
    a = ap.parse_args()
    toks = [t.strip() for t in a.tokens.split(",") if t.strip()]
    asyncio.run(PolyWS(toks, publish=not a.no_publish).run(a.seconds))


if __name__ == "__main__":
    main()
