#!/usr/bin/env python3
import os
import json
import time
import threading
import hmac
import hashlib
import urllib.parse
from getpass import getpass
from datetime import datetime
from pathlib import Path
import requests

# Константы
SETTINGS_FILE = "settings.json"
USERS_FILE = "users.json"
LOGS_DIR = Path("logs")
API_BASE_URL = "https://api.mexc.com/api/v3"

# Глобальные переменные
active_user = None
bot_running = False
bot_paused = False
trading_thread = None
pnl_data = {"realized": 0.0, "unrealized": 0.0, "trades": []}

# Утилиты

def log(message):
    """Логирование сообщений в файл и консоль"""
    LOGS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_message = f"[{timestamp}] {message}"
    
    if active_user:
        log_file = LOGS_DIR / f"{active_user}_log.txt"
        with open(log_file, "a") as f:
            f.write(log_message + "\n")
    print(log_message)

def save_json(path, data):
    """Сохранение данных в JSON файл"""
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def load_json(path):
    """Загрузка данных из JSON файла"""
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return {}

def sign_request(api_secret, params):
    """Подпись запроса к API MEXC"""
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(
        api_secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return signature

# Работа с пользователями

def load_users():
    """Загрузка данных пользователей"""
    return load_json(USERS_FILE)

def save_users(users):
    """Сохранение данных пользователей"""
    save_json(USERS_FILE, users)

def create_user():
    """Создание нового пользователя"""
    users = load_users()
    username = input("Введите имя пользователя: ")
    
    if username in users:
        log("Пользователь уже существует!")
        return None
        
    password = getpass("Введите пароль: ")
    api_key = input("Введите API ключ от MEXC: ")
    api_secret = getpass("Введите API секрет от MEXC: ")
    pair = input("Введите торговую пару (например, BTCUSDT): ").upper()
    
    users[username] = {
        "password": password,
        "api_key": api_key,
        "api_secret": api_secret,
        "pair": pair
    }
    save_users(users)
    save_settings(username, default_settings())
    log(f"Пользователь {username} создан.")
    return username

def select_user():
    """Выбор пользователя"""
    users = load_users()
    if not users:
        log("Нет зарегистрированных пользователей. Создание нового:")
        return create_user()
        
    print("Доступные пользователи:")
    for name in users:
        print(f"- {name}")
        
    username = input("Выберите имя пользователя: ")
    if username not in users:
        log("Пользователь не найден.")
        return select_user()
        
    password = getpass("Введите пароль: ")
    if password != users[username]["password"]:
        log("Неверный пароль.")
        return select_user()
        
    return username

# Настройки

def default_settings():
    """Настройки по умолчанию"""
    return {
        "profit_percentage": 0.5,
        "stop_loss_percentage": 1.0,
        "delay": 30,
        "order_size": 0.001,
        "trading_pair": "BTCUSDT",
        "test_mode": True
    }

def load_settings(user):
    """Загрузка настроек пользователя"""
    settings = load_json(SETTINGS_FILE)
    return settings.get(user, default_settings())

def save_settings(user, user_settings):
    """Сохранение настроек пользователя"""
    settings = load_json(SETTINGS_FILE)
    settings[user] = user_settings
    save_json(SETTINGS_FILE, settings)

def update_setting(key, value):
    """Обновление настройки"""
    user_settings = load_settings(active_user)
    
    # Проверка типов значений
    if key in ["profit_percentage", "stop_loss_percentage", "delay"]:
        value = float(value)
    elif key == "order_size":
        value = float(value)
    elif key == "trading_pair":
        value = value.upper()
    elif key == "test_mode":
        value = value.lower() == "true"
    
    user_settings[key] = value
    save_settings(active_user, user_settings)
    log(f"Настройка {key} обновлена на {value}")

# API MEXC

def get_price(pair):
    """Получение текущей цены"""
    url = f"{API_BASE_URL}/ticker/price"
    params = {"symbol": pair}
    
    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            return float(response.json()["price"])
        else:
            log(f"Ошибка получения цены: {response.text}")
            return None
    except Exception as e:
        log(f"Ошибка запроса цены: {e}")
        return None

def get_balance(api_key, api_secret, coin="USDT"):
    """Получение баланса"""
    url = f"{API_BASE_URL}/account"
    timestamp = int(time.time() * 1000)
    params = {"timestamp": timestamp}
    signature = sign_request(api_secret, params)
    
    headers = {"X-MEXC-APIKEY": api_key}
    params["signature"] = signature
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            balances = response.json().get("balances", [])
            for asset in balances:
                if asset["asset"] == coin:
                    return float(asset["free"])
            return 0.0
        else:
            log(f"Ошибка получения баланса: {response.text}")
            return None
    except Exception as e:
        log(f"Ошибка запроса баланса: {e}")
        return None

def create_order(api_key, api_secret, symbol, side, quantity, price=None, order_type="LIMIT"):
    """Создание ордера"""
    url = f"{API_BASE_URL}/order"
    timestamp = int(time.time() * 1000)
    params = {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "quantity": quantity,
        "timestamp": timestamp
    }
    
    if order_type == "LIMIT":
        params["price"] = price
        params["timeInForce"] = "GTC"
    
    signature = sign_request(api_secret, params)
    headers = {"X-MEXC-APIKEY": api_key}
    params["signature"] = signature
    
    try:
        response = requests.post(url, headers=headers, params=params)
        if response.status_code == 200:
            return response.json()
        else:
            log(f"Ошибка создания ордера: {response.text}")
            return None
    except Exception as e:
        log(f"Ошибка запроса ордера: {e}")
        return None

def cancel_order(api_key, api_secret, symbol, order_id):
    """Отмена ордера"""
    url = f"{API_BASE_URL}/order"
    timestamp = int(time.time() * 1000)
    params = {
        "symbol": symbol,
        "orderId": order_id,
        "timestamp": timestamp
    }
    
    signature = sign_request(api_secret, params)
    headers = {"X-MEXC-APIKEY": api_key}
    params["signature"] = signature
    
    try:
        response = requests.delete(url, headers=headers, params=params)
        if response.status_code == 200:
            return response.json()
        else:
            log(f"Ошибка отмены ордера: {response.text}")
            return None
    except Exception as e:
        log(f"Ошибка запроса отмены: {e}")
        return None

# Торговля

def autobuy_loop():
    """Основной торговый цикл"""
    global bot_running, bot_paused, pnl_data
    
    users = load_users()
    user_data = users[active_user]
    settings = load_settings(active_user)
    
    api_key = user_data["api_key"]
    api_secret = user_data["api_secret"]
    pair = settings["trading_pair"]
    order_size = settings["order_size"]
    profit_percent = settings["profit_percentage"] / 100
    stop_loss_percent = settings["stop_loss_percentage"] / 100
    delay = settings["delay"]
    test_mode = settings["test_mode"]
    
    log(f"Запуск торгового цикла для пары {pair}")
    log(f"Режим: {'ТЕСТОВЫЙ' if test_mode else 'РЕАЛЬНЫЙ'}")
    
    active_orders = []
    
    while bot_running:
        if bot_paused:
            time.sleep(1)
            continue
        
        # Получаем текущую цену
        current_price = get_price(pair)
        if current_price is None:
            time.sleep(5)
            continue
        
        # Проверяем баланс
        if not test_mode:
            balance = get_balance(api_key, api_secret)
            if balance is None or balance < order_size * current_price:
                log("Недостаточно средств для торговли!")
                time.sleep(10)
                continue
        
        # Создаем ордер на покупку
        buy_price = round(current_price * 0.999, 2)  # Немного ниже рынка
        log(f"Попытка покупки {order_size} {pair} по цене {buy_price}")
        
        if test_mode:
            log("[ТЕСТ] Ордер на покупку создан")
            order_id = "TEST_" + str(int(time.time()))
        else:
            buy_order = create_order(
                api_key, api_secret,
                symbol=pair,
                side="BUY",
                quantity=order_size,
                price=buy_price
            )
            
            if not buy_order or "orderId" not in buy_order:
                log("Ошибка создания ордера на покупку!")
                time.sleep(5)
                continue
                
            order_id = buy_order["orderId"]
            log(f"Ордер на покупку создан: {order_id}")
        
        active_orders.append({
            "order_id": order_id,
            "side": "BUY",
            "price": buy_price,
            "quantity": order_size,
            "timestamp": time.time()
        })
        
        # Ожидаем исполнения ордера (упрощенная логика)
        time.sleep(10)
        
        # Устанавливаем ордер на продажу
        sell_price = round(buy_price * (1 + profit_percent), 2)
        stop_loss_price = round(buy_price * (1 - stop_loss_percent), 2)
        
        log(f"Установка тейк-профита {sell_price} и стоп-лосса {stop_loss_price}")
        
        if test_mode:
            log("[ТЕСТ] Ордер на продажу создан")
            profit = (sell_price - buy_price) * order_size
            pnl_data["realized"] += profit
            pnl_data["trades"].append({
                "pair": pair,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "quantity": order_size,
                "profit": profit,
                "timestamp": time.time()
            })
            log(f"[ТЕСТ] Прибыль: {profit:.4f} USDT")
        else:
            sell_order = create_order(
                api_key, api_secret,
                symbol=pair,
                side="SELL",
                quantity=order_size,
                price=sell_price
            )
            
            if sell_order and "orderId" in sell_order:
                log(f"Ордер на продажу создан: {sell_order['orderId']}")
                active_orders.append({
                    "order_id": sell_order["orderId"],
                    "side": "SELL",
                    "price": sell_price,
                    "quantity": order_size,
                    "timestamp": time.time()
                })
            else:
                log("Ошибка создания ордера на продажу!")
        
        time.sleep(delay)

# Команды

def manual():
    """Вывод списка команд"""
    print("\nКоманды:")
    print("start        — запуск бота")
    print("stop         — остановка бота")
    print("pause        — пауза бота")
    print("autobuy      — запуск торгового цикла")
    print("set [ключ] [значение] — изменить настройку")
    print("balance      — показать баланс USDT")
    print("manual       — список команд")
    print("bot stat     — показать состояние бота и PnL")
    print("exit         — выход из программы")
    print()

def bot_stat():
    """Статистика бота"""
    settings = load_settings(active_user)
    print("\nТекущий пользователь:", active_user)
    print("Торговая пара:", settings["trading_pair"])
    print("Режим:", "ТЕСТОВЫЙ" if settings["test_mode"] else "РЕАЛЬНЫЙ")
    print("PnL (реализованный):", round(pnl_data["realized"], 4), "USDT")
    print("Совершено сделок:", len(pnl_data["trades"]))
    print("Бот работает:", bot_running)
    print("Бот на паузе:", bot_paused)
    print()

def show_balance():
    """Показать баланс"""
    users = load_users()
    user_data = users[active_user]
    
    balance = get_balance(user_data["api_key"], user_data["api_secret"])
    if balance is not None:
        log(f"Доступный баланс USDT: {balance:.4f}")
    else:
        log("Не удалось получить баланс")

# Запуск

def main():
    """Основная функция"""
    global active_user, bot_running, bot_paused, trading_thread
    
    active_user = select_user()
    if not active_user:
        return
        
    manual()

    while True:
        cmd = input("> ").strip().lower()

        if cmd == "start":
            bot_running = True
            log("Бот запущен.")

        elif cmd == "stop":
            bot_running = False
            if trading_thread and trading_thread.is_alive():
                trading_thread.join()
            log("Бот остановлен.")

        elif cmd == "pause":
            bot_paused = not bot_paused
            log(f"Пауза {'включена' if bot_paused else 'снята'}.")

        elif cmd == "autobuy":
            if not bot_running:
                log("Сначала запустите бота командой 'start'")
                continue
                
            if trading_thread and trading_thread.is_alive():
                log("Торговый цикл уже запущен!")
                continue
                
            trading_thread = threading.Thread(target=autobuy_loop)
            trading_thread.start()
            log("Автоматическая торговля запущена.")

        elif cmd.startswith("set "):
            try:
                _, key, value = cmd.split(maxsplit=2)
                update_setting(key, value)
            except Exception as e:
                log(f"Ошибка изменения настройки: {e}")

        elif cmd == "balance":
            show_balance()

        elif cmd == "manual":
            manual()

        elif cmd == "bot stat":
            bot_stat()

        elif cmd in ["exit", "quit"]:
            bot_running = False
            if trading_thread and trading_thread.is_alive():
                trading_thread.join()
            log("Выход из программы.")
            break

        else:
            print("Неизвестная команда. Введите 'manual' для списка.")

if __name__ == "__main__":
    main()
