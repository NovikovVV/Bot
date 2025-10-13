import asyncio, time
from dataclasses import dataclass
from app.database import storage
from app.utils import decrypt
import requests

API_BASE = 'https://api.mexc.com'

class MexcClient:
    def __init__(self, api_key='', api_secret='', simulate=True):
        self.api_key = api_key or ''
        self.api_secret = api_secret or ''
        self.simulate = bool(simulate)
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({'X-MEXC-APIKEY': self.api_key})

    def ticker_price(self, symbol):
        r = self.session.get(f"{API_BASE}/api/v3/ticker/price", params={'symbol': symbol})
        r.raise_for_status()
        d = r.json()
        if isinstance(d, dict) and 'price' in d:
            return float(d['price'])
        if isinstance(d, list) and d and 'price' in d[0]:
            return float(d[0]['price'])
        raise RuntimeError('Unexpected ticker')

    def market_buy_quote(self, symbol, quote_qty):
        price = self.ticker_price(symbol)
        qty = quote_qty / price if price>0 else 0.0
        return {'orderId': f'test-buy-{int(time.time()*1000)}', 'fills':[{'price': str(price),'qty': str(qty)}], 'simulated': True}

    def market_sell_base(self, symbol, qty):
        price = self.ticker_price(symbol)
        return {'orderId': f'test-sell-{int(time.time()*1000)}', 'fills':[{'price': str(price),'qty': str(qty)}], 'simulated': True}

@dataclass
class Settings:
    symbol: str = 'BTCUSDT'
    profit_pct: float = 0.5
    drop_pct: float = 1.0
    delay_sec: int = 30
    order_usd: float = 5.0
    mode: str = 'TEST'
    deposit_pct: float = 100.0
    deposit_usdt: float = 1000.0

class Bot:
    def __init__(self, user_row):
        self.user = user_row
        self.settings = Settings(symbol=user_row['symbol'], profit_pct=float(user_row['profit_pct']), drop_pct=float(user_row['drop_pct']), delay_sec=int(user_row['delay_sec']), order_usd=float(user_row['order_usd']), mode=user_row['mode'], deposit_pct=float(user_row['deposit_pct']), deposit_usdt=float(user_row['deposit_usdt']))
        api_key = decrypt(user_row['api_key_enc']) if user_row['api_key_enc'] else ''
        api_secret = decrypt(user_row['api_secret_enc']) if user_row['api_secret_enc'] else ''
        self.client = MexcClient(api_key, api_secret, simulate=(self.settings.mode.upper()=='TEST'))
        self.running = False
        self.paused = False
        self._task = None
        self._last_buy_ref_price = None

    async def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _available_balance(self):
        # in TEST mode we use deposit_usdt as available; in LIVE implement real balance fetch
        return float(self.settings.deposit_usdt) if self.settings.mode.upper()=='TEST' else 0.0

    def _allowed_order_usd(self):
        avail = self._available_balance()
        pct = float(self.settings.deposit_pct)/100.0
        return avail * pct

    async def _loop(self):
        try:
            await self._maybe_initial_buy()
            while self.running:
                if self.paused:
                    await asyncio.sleep(1)
                    continue
                await self._tick()
                await asyncio.sleep(1)
        except Exception as e:
            print('bot loop error', e)

    async def _maybe_initial_buy(self):
        await self._market_buy_new_lot(min(self.settings.order_usd, self._allowed_order_usd()))

    async def _tick(self):
        symbol = self.settings.symbol
        px = self.client.ticker_price(symbol)
        open_lots = storage.open_lots(self.user['id']) or []
        for lot_id, buy_price, qty, target_price in open_lots:
            if px >= target_price:
                await self._market_sell_lot(lot_id, qty, px)
                await asyncio.sleep(self.settings.delay_sec)
                await self._market_buy_new_lot(min(self.settings.order_usd, self._allowed_order_usd()))
                px = self.client.ticker_price(symbol)
        ref = self._last_buy_ref_price
        if ref is None and open_lots:
            ref = max(l[1] for l in open_lots)
            self._last_buy_ref_price = ref
        if ref is not None:
            threshold = ref * (1 - self.settings.drop_pct/100.0)
            if px <= threshold:
                await self._market_buy_new_lot(min(self.settings.order_usd, self._allowed_order_usd()))
                self._last_buy_ref_price = px

    async def _market_buy_new_lot(self, quote_amount):
        allowed = self._allowed_order_usd()
        if quote_amount > allowed:
            print('Order amount exceeds deposit_pct allowed, skipping buy')
            return
        if quote_amount > self._available_balance():
            print('Insufficient available balance to place buy, skipping')
            return
        try:
            order = self.client.market_buy_quote(self.settings.symbol, quote_amount)
            fills = order.get('fills') or []
            qty = 0.0; cost = 0.0
            for f in fills:
                q = float(f.get('qty') or 0); p = float(f.get('price') or 0)
                qty += q; cost += p*q
            buy_price = (cost/qty) if qty>0 else self.client.ticker_price(self.settings.symbol)
            target = buy_price * (1 + self.settings.profit_pct/100.0)
            is_test = bool(order.get('simulated', False))
            storage.add_order(self.user['id'], 'BUY', 'MARKET', self.settings.symbol, buy_price, qty or None, quote_amount, str(order.get('orderId')), is_test, order)
            storage.add_lot(self.user['id'], self.settings.symbol, buy_price, qty if qty>0 else quote_amount/buy_price, target, str(order.get('orderId')), extra={'simulated': is_test})
            self._last_buy_ref_price = buy_price
            print(f'BUY {self.settings.symbol}: qty={qty} price={buy_price} TP={target} (test={is_test})')
        except Exception as e:
            print('BUY error', e)

    async def _market_sell_lot(self, lot_id, qty, mark_price):
        try:
            order = self.client.market_sell_base(self.settings.symbol, qty)
            price = mark_price
            fills = order.get('fills') or []
            if fills:
                proceeds = sum(float(f.get('qty'))*float(f.get('price')) for f in fills)
                sold = sum(float(f.get('qty')) for f in fills)
                if sold>0:
                    price = proceeds/sold
            is_test = bool(order.get('simulated', False))
            storage.add_order(self.user['id'], 'SELL', 'MARKET', self.settings.symbol, price, qty, None, str(order.get('orderId')), is_test, order)
            storage.close_lot(lot_id, price, str(order.get('orderId')), extra={'simulated': is_test})
            print(f'SELL {self.settings.symbol}: qty={qty} price={price} (test={is_test})')
        except Exception as e:
            print('SELL error', e)
