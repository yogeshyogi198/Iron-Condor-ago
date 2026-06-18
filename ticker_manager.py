import threading
import time
import logging
from kiteconnect import KiteTicker

logger = logging.getLogger(__name__)


class TickerManager:
    """Manages a Kite WebSocket ticker connection to stream IV data."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ws = None
        self._running = False
        self._thread = None
        self._api_key = None
        self._access_token = None
        self._subscribed = set()
        self._token_to_index = {}
        self.iv_data = {}
        self._mode_full = None

    def configure(self, api_key: str, access_token: str):
        with self._lock:
            self._api_key = api_key
            self._access_token = access_token

    def start(self):
        with self._lock:
            if self._running:
                return
            if not self._api_key or not self._access_token:
                logger.warning("TickerManager: not configured")
                return
            self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="kite-ticker"
        )
        self._thread.start()
        logger.info("TickerManager: started")

    def stop(self):
        with self._lock:
            self._running = False
            ws = self._ws
            self._ws = None
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    def _run_loop(self):
        while self._running:
            try:
                self._connect()
            except Exception as e:
                logger.error("TickerManager: %s", e)
            for _ in range(50):
                if not self._running:
                    return
                time.sleep(0.1)

    def _connect(self):
        with self._lock:
            ak = self._api_key
            at = self._access_token
        ws = KiteTicker(ak, at)

        def on_ticks(ws, ticks):
            with self._lock:
                for tick in ticks:
                    token = tick.get("instrument_token")
                    if token in self._token_to_index:
                        iv = tick.get("implied_volatility")
                        if iv is not None:
                            try:
                                idx = self._token_to_index[token]
                                self.iv_data[idx] = round(float(iv) * 100, 2)
                            except (TypeError, ValueError):
                                pass

        def on_open(ws):
            logger.info("TickerManager: connected")
            with self._lock:
                tokens = list(self._subscribed)
                mf = self._mode_full
            if tokens and mf is not None:
                try:
                    ws.subscribe(tokens)
                    ws.set_mode(mf, tokens)
                except Exception as e:
                    logger.error("TickerManager: subscribe error: %s", e)

        def on_error(ws, error):
            logger.error("TickerManager: error: %s", error)

        def on_close(ws, code, reason):
            logger.info("TickerManager: closed: %s %s", code, reason)

        ws.on_ticks = on_ticks
        ws.on_open = on_open
        ws.on_error = on_error
        ws.on_close = on_close

        with self._lock:
            self._ws = ws
            try:
                self._mode_full = ws.MODE_FULL
            except AttributeError:
                self._mode_full = "full"

        ws.connect()

    def update_subscriptions(self, token_map: dict):
        """
        token_map: {'nifty': token_int|None, 'banknifty': token_int|None, 'sensex': token_int|None}
        Tokens with None value are skipped.
        """
        new_map = {}
        new_set = set()
        for idx, tok in token_map.items():
            if tok is not None:
                new_set.add(tok)
                new_map[tok] = idx

        with self._lock:
            old_set = self._subscribed.copy()
            old_tok_to_index = self._token_to_index.copy()
            to_add = new_set - old_set
            to_remove = old_set - new_set
            self._subscribed = new_set
            self._token_to_index = new_map
            ws = self._ws

        # Clear stale IV for removed tokens
        for tok in to_remove:
            idx = old_tok_to_index.get(tok)
            if idx:
                with self._lock:
                    self.iv_data.pop(idx, None)

        # Update live connection if connected
        if ws is not None:
            try:
                connected = ws.is_connected()
            except Exception:
                connected = False
            if connected:
                if to_remove:
                    try:
                        ws.unsubscribe(list(to_remove))
                    except Exception:
                        pass
                if to_add:
                    try:
                        ws.subscribe(list(to_add))
                        with self._lock:
                            mf = self._mode_full
                        if mf:
                            ws.set_mode(mf, list(to_add))
                    except Exception as e:
                        logger.error("TickerManager: add sub error: %s", e)

    def get_iv(self, index_name: str):
        with self._lock:
            return self.iv_data.get(index_name)

    def is_connected(self) -> bool:
        with self._lock:
            ws = self._ws
        if ws is None:
            return False
        try:
            return ws.is_connected()
        except Exception:
            return False
