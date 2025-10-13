from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.database import storage
from app.auth import create_user, verify_password
from app.utils import FERNET, decrypt
from app.bot_logic import Bot, MexcClient
from pathlib import Path
import os, sqlite3, time, asyncio

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / 'app' / 'data'
LOG_PATH = DATA_DIR / 'bot.log'
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title='MEXC AutoBot Final')
app.mount('/static', StaticFiles(directory=str(Path(__file__).resolve().parent / 'static')), name='static')
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / 'templates'))

SESSION_COOKIE = 'session_final'

def make_session_cookie(user_id: int) -> str:
    payload = f"{user_id}:{int.from_bytes(os.urandom(4),'big')}"
    return FERNET.encrypt(payload.encode()).decode()

def read_session_cookie(request: Request):
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    try:
        data = FERNET.decrypt(cookie.encode()).decode()
        return int(data.split(':')[0])
    except Exception:
        return None

bots = {}

def log(msg: str):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line)
    with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(line + '\n')

@app.get('/', response_class=HTMLResponse)
async def index(request: Request):
    uid = read_session_cookie(request)
    if not uid:
        return RedirectResponse('/login', status_code=303)
    return RedirectResponse(f'/user/{uid}', status_code=303)

@app.get('/login', response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse('login.html', {'request': request})

@app.post('/login')
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    row = storage.get_user_by_username(username)
    if not row:
        return HTMLResponse('Invalid credentials', status_code=401)
    uid = row['id']
    if not verify_password(uid, password):
        return HTMLResponse('Invalid credentials', status_code=401)
    token = make_session_cookie(uid)
    resp = RedirectResponse(f'/user/{uid}', status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite='lax')
    return resp

@app.get('/register', response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse('register.html', {'request': request})

@app.post('/register')
async def register(username: str = Form(...), password: str = Form(...), api_key: str = Form(''), api_secret: str = Form(''), symbol: str = Form('BTCUSDT'), mode: str = Form('TEST'), deposit_pct: float = Form(100.0), deposit_usdt: float = Form(1000.0)):
    try:
        uid = create_user(username, password, api_key, api_secret, symbol, mode.upper(), deposit_pct, float(deposit_usdt))
    except sqlite3.IntegrityError:
        return HTMLResponse('Username taken', status_code=409)
    return RedirectResponse('/login', status_code=303)

@app.get('/user/{user_id}', response_class=HTMLResponse)
async def user_page(request: Request, user_id: int):
    uid = read_session_cookie(request)
    if not uid or uid != user_id:
        return RedirectResponse('/login', status_code=303)
    user_row = storage.get_user_row(user_id)
    if user_id not in bots:
        bots[user_id] = Bot(user_row)
    bot = bots[user_id]
    status_html = '<p>Running</p>' if bot.running else '<p>Stopped</p>'
    return templates.TemplateResponse('dashboard.html', {'request': request, 'user': user_row, 'status_html': status_html})

@app.post('/start')
async def start_bot(user_id: int = Form(...)):
    user_row = storage.get_user_row(user_id)
    if not user_row:
        return RedirectResponse('/login', status_code=303)
    if user_id not in bots:
        bots[user_id] = Bot(user_row)
    await bots[user_id].start()
    return RedirectResponse(f'/user/{user_id}', status_code=303)

@app.post('/stop')
async def stop_bot(user_id: int = Form(...)):
    if user_id in bots:
        await bots[user_id].stop()
        del bots[user_id]
    return RedirectResponse(f'/user/{user_id}', status_code=303)

@app.get('/orders/{user_id}/{filter_type}')
async def orders_api(user_id: int, filter_type: str):
    if filter_type == 'active':
        rows = storage.open_lots(user_id)
    elif filter_type == 'completed':
        rows = [r for r in storage.lots_for_user(user_id) if r['status']=='SOLD']
    else:
        rows = storage.lots_for_user(user_id)
    out = [dict(r) for r in rows]
    return JSONResponse(out)

@app.get('/balance/{user_id}')
async def balance_api(user_id: int):
    row = storage.get_user_row(user_id)
    api_key = decrypt(row['api_key_enc']) if row['api_key_enc'] else ''
    api_secret = decrypt(row['api_secret_enc']) if row['api_secret_enc'] else ''
    client = MexcClient(api_key, api_secret, simulate=(row['mode'].upper()=='TEST'))
    try:
        price = client.ticker_price(row['symbol'])
    except Exception:
        price = None
    available = float(row['deposit_usdt']) if row['mode'].upper()=='TEST' else 0.0
    return JSONResponse({'symbol': row['symbol'], 'price': price, 'available_usdt': available})
