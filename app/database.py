from pathlib import Path
import sqlite3, time, json
BASE = Path(__file__).resolve().parent
DATA = BASE / 'data'
DATA.mkdir(exist_ok=True)
DB = DATA / 'bot.db'

class Storage:
    def __init__(self, path=DB):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL;')
        self._init()

    def _init(self):
        cur = self.conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS users (
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
            mode TEXT NOT NULL DEFAULT 'TEST',
            deposit_pct REAL NOT NULL DEFAULT 100.0,
            deposit_usdt REAL NOT NULL DEFAULT 1000.0
        );""")
        cur.execute("""CREATE TABLE IF NOT EXISTS lots (
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
            extra TEXT
        );""")
        cur.execute("""CREATE TABLE IF NOT EXISTS orders (
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
            extra TEXT
        );""")
        self.conn.commit()

    def create_user(self, username, pwd_blob, api_key_enc, api_secret_enc, symbol, mode, deposit_pct, deposit_usdt):
        cur = self.conn.execute("""INSERT INTO users(username,pwd_pbkdf2,api_key_enc,api_secret_enc,symbol,mode,deposit_pct,deposit_usdt)
            VALUES(?,?,?,?,?,?,?,?)""", (username,pwd_blob,api_key_enc,api_secret_enc,symbol.upper(),mode.upper(), float(deposit_pct), float(deposit_usdt)))
        self.conn.commit()
        return cur.lastrowid

    def get_user_by_username(self, username):
        cur = self.conn.execute('SELECT id, username FROM users WHERE username=?', (username,))
        return cur.fetchone()

    def get_user_row(self, user_id):
        cur = self.conn.execute('SELECT * FROM users WHERE id=?', (user_id,))
        return cur.fetchone()

    def update_setting(self, user_id, field, value):
        self.conn.execute(f'UPDATE users SET {field}=? WHERE id=?', (value, user_id))
        self.conn.commit()

    def add_order(self, user_id, side, type_, symbol, price, qty, quote_qty, exchange_id, is_test, extra):
        self.conn.execute('INSERT INTO orders(user_id,ts,side,type,symbol,price,qty,quote_qty,exchange_order_id,is_test,extra) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                          (user_id, int(time.time()*1000), side, type_, symbol, price, qty, quote_qty, exchange_id, 1 if is_test else 0, json.dumps(extra or {})))
        self.conn.commit()

    def add_lot(self, user_id, symbol, buy_price, qty, target_price, buy_order_id, extra=None):
        self.conn.execute('INSERT INTO lots(user_id,symbol,buy_order_id,buy_time,buy_price,qty,target_price,status,extra) VALUES(?,?,?,?,?,?,?,?,?)',
                          (user_id, symbol, buy_order_id, int(time.time()*1000), buy_price, qty, target_price, 'OPEN', json.dumps(extra or {})))
        self.conn.commit()

    def close_lot(self, lot_id, sell_price, sell_order_id, extra=None):
        self.conn.execute('UPDATE lots SET status=?, sell_time=?, sell_price=?, sell_order_id=?, extra=COALESCE(extra, "{}") || ? WHERE id=?',
                          ('SOLD', int(time.time()*1000), sell_price, sell_order_id, json.dumps(extra or {}), lot_id))
        self.conn.commit()

    def open_lots(self, user_id):
        cur = self.conn.execute('SELECT id, buy_price, qty, target_price FROM lots WHERE user_id=? AND status="OPEN" ORDER BY id', (user_id,))
        return cur.fetchall()

    def lots_for_user(self, user_id):
        cur = self.conn.execute('SELECT id, symbol, buy_price, qty, target_price, status, sell_price FROM lots WHERE user_id=? ORDER BY id', (user_id,))
        return cur.fetchall()

    def orders_for_user(self, user_id):
        cur = self.conn.execute('SELECT ts, side, type, symbol, price, qty, quote_qty, exchange_order_id, is_test, extra FROM orders WHERE user_id=? ORDER BY id', (user_id,))
        return cur.fetchall()

storage = Storage()
