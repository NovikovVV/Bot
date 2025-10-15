#!/usr/bin/env python3
"""
MEXC AutoBot Web - final fixed
- FastAPI web UI with simple HTML forms
- Per-user authentication (username/password)
- Encrypted API keys using cryptography.Fernet
- Test mode by default (no real orders)
- Spot market orders (simulated in TEST mode)
- Start / Pause / Resume / Stop controls, settings, logs, order history, PnL
Run:
    pip install fastapi uvicorn[standard] requests cryptography tabulate
    uvicorn mexc_autobot_web_auth_final:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations
import asyncio
import hmac
import hashlib
import json
import os
import signal
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import requests
from cryptography.fernet import Fernet
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, FileResponse
from tabulate import tabulate

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "bot.db"
LOG_PATH = DATA_DIR / "bot.log"
KEY_PATH = DATA_DIR / "encryption.key"
API_BASE = "https://api.mexc.com"

DATA_DIR.mkdir(parents=True, exist_ok=True)
if not KEY_PATH.exists():
    KEY_PATH.write_bytes(Fernet.generate_key())
FERNET = Fernet(KEY_PATH.read_bytes())

def now_ms() -> int:
    return int(time.time() * 1000)

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# --- Storage ---
class Storage:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init()

    def _init(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                pwd_pbkdf2 TEXT NOT NULL,
                api_key_enc TEXT,
                api_secret_enc TEXT,
                symbol TEXT NOT NULL DEFAULT 'BTCUSDT',
                profit_pct REAL NOT NULL DEFAULT 0.5,
                drop_pct REAL NOT NULL DEFAULT 1.0,
                delay_sec INTEGER NOT NULL DEFAULT 30,
                order_usd REAL NOT NULL DEFAULT 5.0,
                mode TEXT NOT NULL DEFAULT 'TEST'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                buy_order_id TEXT,
                buy_time INTEGER NOT NULL,
                buy_price REAL NOT NULL,
                qty REAL NOT NULL,
                target_price REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                sell_order_id TEXT,
                sell_time INTEGER,
                sell_price REAL,
                extra TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ts INTEGER NOT NULL,
                side TEXT NOT NULL,
                type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                price REAL,
                qty REAL,
                quote_qty REAL,
                exchange_order_id TEXT,
                is_test INTEGER NOT NULL DEFAULT 1,
                extra TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        self.conn.commit()

    def create_user(self, username: str, password: str, api_key: Optional[str]=None, api_secret: Optional[str]=None, symbol: str='BTCUSDT', mode: str='TEST') -> int:
        salt = os.urandom(16)
        pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 200_000)
        blob = salt.hex() + ':' + pwd_hash.hex()
        api_key_enc = FERNET.encrypt(api_key.encode()).decode() if api_key else ''
        api_secret_enc = FERNET.encrypt(api_secret.encode()).decode() if api_secret else ''
        cur = self.conn.execute(
            "INSERT INTO users(username, pwd_pbkdf2, api_key_enc, api_secret_enc, symbol, mode) VALUES(?,?,?,?,?,?)",
            (username, blob, api_key_enc, api_secret_enc, symbol.upper(), mode.upper())
        )
        self.conn.commit()
        return cur.lastrowid

    def get_user_by_username(self, username: str):
        cur = self.conn.execute("SELECT id, username FROM users WHERE username=?;", (username,))
        return cur.fetchone()

    def verify_password(self, user_id: int, password: str) -> bool:
        cur = self.conn.execute("SELECT pwd_pbkdf2 FROM users WHERE id=?;", (user_id,))
        row = cur.fetchone()
        if not row:
            return False
        salt_hex, hash_hex = row['pwd_pbkdf2'].split(':')
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        check = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 200_000)
        return hmac.compare_digest(expected, check)

    def load_user(self, user_id: int) -> Dict:
        cur = self.conn.execute("SELECT id, username, api_key_enc, api_secret_enc, symbol, profit_pct, drop_pct, delay_sec, order_usd, mode FROM users WHERE id=?;", (user_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError('User not found')
        api_key = FERNET.decrypt(row['api_key_enc'].encode()).decode() if row['api_key_enc'] else ''
        api_secret = FERNET.decrypt(row['api_secret_enc'].encode()).decode() if row['api_secret_enc'] else ''
        return {
            'id': row['id'],
            'username': row['username'],
            'api_key': api_key,
            'api_secret': api_secret,
            'symbol': row['symbol'],
            'profit_pct': row['profit_pct'],
            'drop_pct': row['drop_pct'],
            'delay_sec': row['delay_sec'],
            'order_usd': row['order_usd'],
            'mode': row['mode']
        }

    def update_setting(self, user_id: int, field: str, value):
        if field not in {'profit_pct','drop_pct','delay_sec','order_usd','symbol','mode','api_key','api_secret'}:
            raise ValueError('Unknown setting')
        if field == 'api_key' and value is not None:
            value = FERNET.encrypt(value.encode()).decode()
        if field == 'api_secret' and value is not None:
            value = FERNET.encrypt(value.encode()).decode()
        self.conn.execute(f"UPDATE users SET {field}=? WHERE id=?;", (value, user_id))
        self.conn.commit()

    def add_order(self, user_id: int, side: str, type_: str, symbol: str, price: Optional[float], qty: Optional[float], quote_qty: Optional[float], exchange_id: Optional[str], is_test: bool, extra: dict):
        self.conn.execute(
            "INSERT INTO orders(user_id, ts, side, type, symbol, price, qty, quote_qty, exchange_order_id, is_test, extra) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, now_ms(), side, type_, symbol, price, qty, quote_qty, exchange_id, 1 if is_test else 0, json.dumps(extra or {}))
        )
        self.conn.commit()

    def open_lots(self, user_id: int):
        cur = self.conn.execute("SELECT id, buy_price, qty, target_price FROM lots WHERE user_id=? AND status='OPEN' ORDER BY id;", (user_id,))
        return cur.fetchall()

    def add_lot(self, user_id: int, symbol: str, buy_price: float, qty: float, target_price: float, buy_order_id: Optional[str], extra: dict=None):
        self.conn.execute(
            "INSERT INTO lots(user_id, symbol, buy_order_id, buy_time, buy_price, qty, target_price, status, extra) VALUES(?,?,?,?,?,?,?,?,?)",
            (user_id, symbol, buy_order_id, now_ms(), buy_price, qty, target_price, 'OPEN', json.dumps(extra or {}))
        )
        self.conn.commit()

    def close_lot(self, lot_id: int, sell_price: float, sell_order_id: Optional[str], extra: dict=None):
        self.conn.execute(
            "UPDATE lots SET status='SOLD', sell_time=?, sell_price=?, sell_order_id=?, extra=COALESCE(extra, '{}') || ? WHERE id=?",
            (now_ms(), sell_price, sell_order_id, json.dumps(extra or {}), lot_id)
        )
        self.conn.commit()

    def lots_for_user(self, user_id: int):
        cur = self.conn.execute("SELECT id, symbol, buy_price, qty, target_price, status, sell_price FROM lots WHERE user_id=? ORDER BY id;", (user_id,))
        return cur.fetchall()

    def orders_for_user(self, user_id: int):
        cur = self.conn.execute("SELECT ts, side, type, symbol, price, qty, quote_qty, exchange_order_id, is_test, extra FROM orders WHERE user_id=? ORDER BY id;", (user_id,))
        return cur.fetchall()

storage = Storage(DB_PATH)

# --- Mexc client (simulate by default) ---
class MexcClient:
    def __init__(self, api_key: str, api_secret: str, simulate: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.simulate = bool(simulate)
        self.session = requests.Session()
        if api_key:
            self.session.headers.update({"X-MEXC-APIKEY": api_key})

    def ticker_price(self, symbol: str) -> float:
        r = self.session.get(f"{API_BASE}/api/v3/ticker/price", params={"symbol": symbol})
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and 'price' in data:
            return float(data['price'])
        if isinstance(data, list) and data and 'price' in data[0]:
            return float(data[0]['price'])
        raise ValueError('Unexpected ticker response')

    def _signature(self, params: dict) -> dict:
        q = '&'.join(f"{k}={params[k]}" for k in sorted(params))
        sig = hmac.new(self.api_secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        params['signature'] = sig
        return params

    def _post(self, path: str, params: dict) -> dict:
    params.setdefault('timestamp', now_ms())
    params.setdefault('recvWindow', 10000)
    signed = self._signature(params)
    if self.simulate:
        price = self.ticker_price(params.get('symbol',''))
        if params.get('side') == 'BUY' and 'quoteOrderQty' in params:
            quote = float(params['quoteOrderQty'])
            qty = quote / price if price>0 else 0.0
            return {'orderId': f"test-buy-{now_ms()}", 'fills':[{'price': str(price),'qty': str(qty)}], 'simulated': True}
        if params.get('side') == 'SELL' and 'quantity' in params:
            qty = float(params['quantity'])
            return {'orderId': f"test-sell-{now_ms()}", 'fills':[{'price': str(price),'qty': str(qty)}], 'simulated': True}
        return {'orderId': f"test-{now_ms()}", 'simulated': True}
    else:
        r = self.session.post(f"{API_BASE}{path}", data=signed)
        try:
            r.raise_for_status()
        except Exception as e:
            log(f"MEXC POST ERROR: {e} | {r.text}")
            raise
        return r.json()

    def market_buy_quote(self, symbol: str, quote_qty: float) -> dict:
        return self._post('/api/v3/order', {'symbol': symbol, 'side': 'BUY', 'type': 'MARKET', 'quoteOrderQty': f"{quote_qty}"})

    def market_sell_base(self, symbol: str, qty: float) -> dict:
        return self._post('/api/v3/order', {'symbol': symbol, 'side': 'SELL', 'type': 'MARKET', 'quantity': f"{qty}"})

# --- Strategy & Bot ---
@dataclass
class Settings:
    symbol: str = 'BTCUSDT'
    profit_pct: float = 0.5
    drop_pct: float = 1.0
    delay_sec: int = 30
    order_usd: float = 5.0
    mode: str = 'TEST'

class Bot:
    def __init__(self, storage: Storage, user: Dict):
        self.db = storage
        self.user = user
        self.settings = Settings(symbol=user['symbol'], profit_pct=float(user['profit_pct']), drop_pct=float(user['drop_pct']), delay_sec=int(user['delay_sec']), order_usd=float(user['order_usd']), mode=user.get('mode','TEST'))
        self.client = MexcClient(user.get('api_key',''), user.get('api_secret',''), simulate=(self.settings.mode.upper()=='TEST'))
        self.running = False
        self.paused = False
        self._task: Optional[asyncio.Task] = None
        self._last_buy_ref_price: Optional[float] = None

    def save_settings(self):
        self.db.update_setting(self.user['id'], 'symbol', self.settings.symbol)
        self.db.update_setting(self.user['id'], 'profit_pct', self.settings.profit_pct)
        self.db.update_setting(self.user['id'], 'drop_pct', self.settings.drop_pct)
        self.db.update_setting(self.user['id'], 'delay_sec', self.settings.delay_sec)
        self.db.update_setting(self.user['id'], 'order_usd', self.settings.order_usd)
        self.db.update_setting(self.user['id'], 'mode', self.settings.mode.upper())
        self.client.simulate = (self.settings.mode.upper()=='TEST')

    async def start(self):
        if self.running:
            log('Bot already running')
            return
        self.running = True
        self.paused = False
        log(f"Starting bot {self.user['username']} (mode={self.settings.mode})")
        self._task = asyncio.create_task(self._run_loop())

    async def pause(self):
        if not self.running: return
        self.paused = True
        log('Paused')

    async def resume(self):
        if not self.running: return
        self.paused = False
        log('Resumed')

    async def stop(self):
        if not self.running: return
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log('Stopped')

    def status_html(self) -> str:
        lots = self.db.open_lots(self.user['id']) or []
        rows = [[l[0], f"{l[1]:.8f}", f"{l[2]:.8f}", f"{l[3]:.8f}"] for l in lots]
        table = tabulate(rows, headers=['lot_id','buy_price','qty','target_price'], tablefmt='html') if rows else '<i>No open lots</i>'
        cfg = {'symbol': self.settings.symbol, 'profit_pct': self.settings.profit_pct, 'drop_pct': self.settings.drop_pct, 'delay_sec': self.settings.delay_sec, 'order_usd': self.settings.order_usd, 'mode': self.settings.mode}
        return f"<h3>Mode: {self.settings.mode}</h3><h3>Settings</h3><pre>{json.dumps(cfg, indent=2)}</pre><h3>Open lots</h3>{table}"

    def pnl_report(self):
        lots = self.db.lots_for_user(self.user['id']) or []
        realized = 0.0
        open_unreal = 0.0
        try:
            px = self.client.ticker_price(self.settings.symbol)
        except Exception:
            px = None
        rows = []
        for (lot_id, symbol, buy_price, qty, target, status, sell_price) in lots:
            if status == 'SOLD' and sell_price is not None:
                realized += (sell_price - buy_price) * qty
            elif status == 'OPEN' and px is not None:
                open_unreal += (px - buy_price) * qty
        for (lot_id, symbol, buy_price, qty, target, status, sell_price) in lots:
            pnl = ((sell_price or px or 0) - buy_price) * qty if (status=='SOLD' or px is not None) else None
            rows.append([lot_id, status, buy_price, qty, target, sell_price or '-', pnl if pnl is not None else '-'])
        html = tabulate(rows, headers=['lot_id','status','buy','qty','target','sell','pnl'], tablefmt='html') if rows else '<i>No lots</i>'
        return html, realized, open_unreal

    def history_html(self):
        orders = self.db.orders_for_user(self.user['id']) or []
        rows = []
        for ts, side, type_, symbol, price, qty, qqty, exid, is_test, extra in orders:
            rows.append([time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts/1000)), side, type_, symbol, price or '-', qty or '-', qqty or '-', exid or '-', 'TEST' if is_test else 'LIVE'])
        return tabulate(rows, headers=['time','side','type','symbol','price','qty','quote_qty','exchange_id','mode'], tablefmt='html') if rows else '<i>No orders</i>'

    async def _run_loop(self):
        try:
            await self._maybe_initial_buy()
            while self.running:
                if self.paused:
                    await asyncio.sleep(1)
                    continue
                await self._tick()
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log(f'Loop error: {e}')

    async def _maybe_initial_buy(self):
        log('Initial market buy (autobuy)')
        await self._market_buy_new_lot(self.settings.order_usd)

    async def _tick(self):
        symbol = self.settings.symbol
        px = self.client.ticker_price(symbol)
        open_lots = self.db.open_lots(self.user['id']) or []

        # TP check
        for lot_id, buy_price, qty, target_price in open_lots:
            if px >= target_price:
                log(f'TP hit for lot {lot_id}: price {px} >= {target_price}. Selling...')
                await self._market_sell_lot(lot_id, qty, px)
                log(f'Waiting {self.settings.delay_sec}s then buying...')
                await asyncio.sleep(self.settings.delay_sec)
                await self._market_buy_new_lot(self.settings.order_usd)
                px = self.client.ticker_price(symbol)

        # Drop-based DCA
        ref = self._last_buy_ref_price
        if ref is None and open_lots:
            ref = max(l[1] for l in open_lots)
            self._last_buy_ref_price = ref
        if ref is not None:
            threshold = ref * (1 - self.settings.drop_pct/100)
            if px <= threshold:
                log(f'Price dropped {self.settings.drop_pct}% from {ref} -> {px} <= {threshold}. DCA buy.')
                await self._market_buy_new_lot(self.settings.order_usd)
                self._last_buy_ref_price = px

    async def _market_buy_new_lot(self, quote_amount: float):
        symbol = self.settings.symbol
        try:
            order = self.client.market_buy_quote(symbol, quote_amount)
            fills = order.get('fills') or []
            qty = 0.0; cost = 0.0
            for f in fills:
                q = float(f.get('qty') or f.get('quantity') or 0)
                p = float(f.get('price') or 0)
                qty += q; cost += p*q
            buy_price = (cost/qty) if qty>0 else self.client.ticker_price(symbol)
            target = buy_price * (1 + self.settings.profit_pct/100)
            is_test = bool(order.get('simulated', False))
            self.db.add_order(self.user['id'], 'BUY', 'MARKET', symbol, buy_price, qty or None, quote_amount, str(order.get('orderId')), is_test, order)
            self.db.add_lot(self.user['id'], symbol, buy_price, qty if qty>0 else quote_amount/buy_price, target, str(order.get('orderId')), extra={'simulated': is_test})
            self._last_buy_ref_price = buy_price
            log(f"BUY {symbol}: ~{qty:.8f} @ {buy_price:.8f}; TP {target:.8f} (test={is_test})")
        except Exception as e:
            log(f'BUY error: {e}')

    async def _market_sell_lot(self, lot_id: int, qty: float, mark_price: float):
        symbol = self.settings.symbol
        try:
            order = self.client.market_sell_base(symbol, qty)
            price = mark_price
            fills = order.get('fills') or []
            if fills:
                proceeds = 0.0; sold = 0.0
                for f in fills:
                    q = float(f.get('qty') or f.get('quantity') or 0)
                    p = float(f.get('price') or 0)
                    proceeds += p*q; sold += q
                if sold>0:
                    price = proceeds/sold
            is_test = bool(order.get('simulated', False))
            self.db.add_order(self.user['id'], 'SELL', 'MARKET', symbol, price, qty, None, str(order.get('orderId')), is_test, order)
            self.db.close_lot(lot_id, price, str(order.get('orderId')), extra={'simulated': is_test})
            log(f"SELL {symbol}: {qty:.8f} @ {price:.8f} (test={is_test})")
        except Exception as e:
            log(f'SELL error: {e}')

# --- Web app & auth helpers ---
app = FastAPI(title='MEXC AutoBot - Authenticated')

bots: Dict[int, Bot] = {}
SESSION_COOKIE = "session"

def make_session_cookie(user_id: int) -> str:
    payload = json.dumps({"user_id": user_id, "ts": now_ms()}).encode()
    return FERNET.encrypt(payload).decode()

def read_session_cookie(request: Request) -> Optional[int]:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    try:
        data = FERNET.decrypt(cookie.encode()).decode()
        obj = json.loads(data)
        return int(obj.get("user_id"))
    except Exception:
        return None

def require_user(request: Request) -> Optional[Dict]:
    uid = read_session_cookie(request)
    if not uid:
        return None
    try:
        return storage.load_user(uid)
    except Exception:
        return None

@app.get('/', response_class=HTMLResponse)
async def index(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse(url='/login', status_code=303)
    return RedirectResponse(url=f'/user/{user["id"]}', status_code=303)

@app.get('/login', response_class=HTMLResponse)
async def login_form():
    return HTMLResponse("""
    <h1>Login</h1>
    <form method='post' action='/login'>
      username: <input name='username'><br>
      password: <input name='password' type='password'><br>
      <button type='submit'>Login</button>
    </form>
    <p>Or <a href='/register'>register</a></p>
    """)

@app.post('/login')
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    row = storage.get_user_by_username(username)
    if not row:
        return HTMLResponse('<p>Invalid credentials</p><p><a href="/login">Back</a></p>')
    uid = row['id']
    if not storage.verify_password(uid, password):
        return HTMLResponse('<p>Invalid credentials</p><p><a href="/login">Back</a></p>')
    token = make_session_cookie(uid)
    response = RedirectResponse(url=f'/user/{uid}', status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite='lax')
    return response

@app.get('/logout')
async def logout(response: Response):
    response = RedirectResponse(url='/login', status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response

@app.get('/register', response_class=HTMLResponse)
async def register_form():
    return HTMLResponse("""
    <h1>Register</h1>
    <form method='post' action='/register'>
      username: <input name='username'><br>
      password: <input name='password' type='password'><br>
      api_key: <input name='api_key'><br>
      api_secret: <input name='api_secret' type='password'><br>
      symbol: <input name='symbol' value='BTCUSDT'><br>
      mode: <select name='mode'><option value='TEST'>TEST</option><option value='LIVE'>LIVE</option></select><br>
      <button type='submit'>Create account</button>
    </form>
    <p>Already have account? <a href='/login'>Login</a></p>
    """)

@app.post('/register')
async def register(username: str = Form(...), password: str = Form(...), api_key: str = Form(''), api_secret: str = Form(''), symbol: str = Form('BTCUSDT'), mode: str = Form('TEST')):
    try:
        uid = storage.create_user(username, password, api_key, api_secret, symbol, mode.upper())
    except sqlite3.IntegrityError:
        return HTMLResponse('<p>Username taken</p><p><a href="/register">Back</a></p>')
    return RedirectResponse(url='/login', status_code=303)

@app.get('/user/{user_id}', response_class=HTMLResponse)
async def page_user(request: Request, user_id: int):
    user = require_user(request)
    if not user or int(user['id']) != int(user_id):
        return RedirectResponse(url='/login', status_code=303)
    if user_id not in bots:
        bots[user_id] = Bot(storage, user)
    bot = bots[user_id]
    status_html = bot.status_html()
    pnl_html, realized, unreal = bot.pnl_report()
    hist_html = bot.history_html()
    run_buttons = ''
    if bot.running:
        run_buttons += f"<form method='post' action='/stop'><button name='user_id' value='{user_id}'>Stop</button></form>"
        run_buttons += f"<form method='post' action='/pause'><button name='user_id' value='{user_id}'>Pause</button></form>"
    else:
        run_buttons += f"<form method='post' action='/start'><button name='user_id' value='{user_id}'>Start (autobuy)</button></form>"
    run_buttons += f"<form method='post' action='/resume'><button name='user_id' value='{user_id}'>Resume</button></form>"
    run_buttons += f"<form method='post' action='/toggle_mode'><button name='user_id' value='{user_id}'>Toggle MODE (TEST/LIVE)</button></form>"

    html = f"""
    <h1>User: {user['username']} (id {user_id})</h1>
    <p><a href='/logout'>Logout</a></p>
    {run_buttons}
    <h2>Status</h2>
    {status_html}
    <h2>PnL</h2>
    {pnl_html}
    <p>Realized: {realized:.6f} | Unrealized: {unreal:.6f}</p>
    <h2>History</h2>
    {hist_html}
    <h2>Controls / Settings</h2>
    <form method='post' action='/set'>
      <input type='hidden' name='user_id' value='{user_id}'>
      profit_pct: <input name='profit_pct' value='{user['profit_pct']}'><br>
      drop_pct: <input name='drop_pct' value='{user['drop_pct']}'><br>
      delay_sec: <input name='delay_sec' value='{user['delay_sec']}'><br>
      order_usd: <input name='order_usd' value='{user['order_usd']}'><br>
      symbol: <input name='symbol' value='{user['symbol']}'><br>
      mode: <select name='mode'><option value='TEST' {"selected" if user.get('mode','TEST')=='TEST' else ""}>TEST</option><option value='LIVE' {"selected" if user.get('mode','TEST')=='LIVE' else ""}>LIVE</option></select><br>
      api_key: <input name='api_key' value=''><br>
      api_secret: <input name='api_secret' value=''><br>
      <button type='submit'>Save settings</button>
    </form>
    <p><a href='/logs'>Logs</a> | <a href='/download/db'>Download DB</a></p>
    """
    return HTMLResponse(html)

@app.post('/start')
async def start_bot(user_id: int = Form(...), request: Request = None):
    user = require_user(request)
    if not user or int(user['id']) != int(user_id):
        return RedirectResponse('/login', status_code=303)
    if user_id not in bots:
        bots[user_id] = Bot(storage, user)
    bot = bots[user_id]
    await bot.start()
    return RedirectResponse(url=f'/user/{user_id}', status_code=303)

@app.post('/pause')
async def pause_bot(user_id: int = Form(...), request: Request = None):
    user = require_user(request)
    if not user or int(user['id']) != int(user_id):
        return RedirectResponse('/login', status_code=303)
    if user_id in bots:
        await bots[user_id].pause()
    return RedirectResponse(url=f'/user/{user_id}', status_code=303)

@app.post('/resume')
async def resume_bot(user_id: int = Form(...), request: Request = None):
    user = require_user(request)
    if not user or int(user['id']) != int(user_id):
        return RedirectResponse('/login', status_code=303)
    if user_id in bots:
        await bots[user_id].resume()
    return RedirectResponse(url=f'/user/{user_id}', status_code=303)

@app.post('/stop')
async def stop_bot(user_id: int = Form(...), request: Request = None):
    user = require_user(request)
    if not user or int(user['id']) != int(user_id):
        return RedirectResponse('/login', status_code=303)
    if user_id in bots:
        await bots[user_id].stop()
    return RedirectResponse(url=f'/user/{user_id}', status_code=303)

@app.post('/toggle_mode')
async def toggle_mode(user_id: int = Form(...), request: Request = None):
    user = require_user(request)
    if not user or int(user['id']) != int(user_id):
        return RedirectResponse('/login', status_code=303)
    new_mode = 'LIVE' if user.get('mode','TEST')=='TEST' else 'TEST'
    storage.update_setting(user_id, 'mode', new_mode)
    if user_id in bots:
        bots[user_id] = Bot(storage, storage.load_user(user_id))
    return RedirectResponse(url=f'/user/{user_id}', status_code=303)

@app.post('/set')
async def set_settings(user_id: int = Form(...), profit_pct: float = Form(...), drop_pct: float = Form(...), delay_sec: int = Form(...), order_usd: float = Form(...), symbol: str = Form(...), mode: str = Form(...), api_key: str = Form(''), api_secret: str = Form(''), request: Request = None):
    user = require_user(request)
    if not user or int(user['id']) != int(user_id):
        return RedirectResponse('/login', status_code=303)
    storage.update_setting(user_id, 'profit_pct', float(profit_pct))
    storage.update_setting(user_id, 'drop_pct', float(drop_pct))
    storage.update_setting(user_id, 'delay_sec', int(delay_sec))
    storage.update_setting(user_id, 'order_usd', float(order_usd))
    storage.update_setting(user_id, 'symbol', symbol.upper())
    storage.update_setting(user_id, 'mode', mode.upper())
    if api_key:
        storage.update_setting(user_id, 'api_key', api_key)
    if api_secret:
        storage.update_setting(user_id, 'api_secret', api_secret)
    if user_id in bots:
        bots[user_id] = Bot(storage, storage.load_user(user_id))
    return RedirectResponse(url=f'/user/{user_id}', status_code=303)

@app.get('/logs', response_class=HTMLResponse)
async def view_logs(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if not LOG_PATH.exists():
        return HTMLResponse('<pre>No logs yet.</pre>')
    txt = LOG_PATH.read_text(encoding='utf-8')[-4000:]
    return HTMLResponse(f"<h1>Logs (tail)</h1><pre>{txt}</pre><p><a href='/user/{user['id']}'>Back</a></p>")

@app.get('/download/db')
async def download_db(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse('/login', status_code=303)
    if DB_PATH.exists():
        return FileResponse(str(DB_PATH), media_type='application/x-sqlite3', filename='bot.db')
    return PlainTextResponse('No DB found', status_code=404)

# Graceful shutdown
def _shutdown_handlers():
    for uid, b in list(bots.items()):
        if b.running:
            try:
                asyncio.get_event_loop().create_task(b.stop())
            except Exception:
                pass

for sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(sig, lambda s, f: _shutdown_handlers())
    except Exception:
        pass

if __name__ == '__main__':
    import uvicorn
    uvicorn.run('mexc_autobot_web_auth_final:app', host='0.0.0.0', port=8000, reload=False)
