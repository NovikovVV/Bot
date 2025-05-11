#!/usr/bin/env python3
import os
import json
import time
import threading
from getpass import getpass
from datetime import datetime
from pathlib import Path
import requests

# Константы
SETTINGS_FILE = "settings.json"
USERS_FILE = "users.json"
LOGS_DIR = Path("logs")

# Глобальные переменные
active_user = None
bot_running = False
bot_paused = False
trading_thread = None
pnl_data = {"realized": 0.0, "unrealized": 0.0, "trades": []}

# Утилиты

def log(message):
    LOGS_DIR.mkdir(exist_ok=True)
    if active_user:
        log_file = LOGS_DIR / f"{active_user}_log.txt"
        with open(log_file, "a") as f:
            f.write(f"[{datetime.now()}] {message}\n")
    print(f"[LOG] {message}")

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def load_json(path):
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return {}

# Работа с пользователями

def load_users():
    return load_json(USERS_FILE)

def save_users(users):
    save_json(USERS_FILE, users)

def create_user():
    users = load_users()
    username = input("Введите имя пользователя: ")
    password = getpass("Введите пароль: ")
    api_key = input("Введите API ключ от MEXC: ")
    api_secret = input("Введите API секрет от MEXC: ")
    pair = input("Введите торговую пару (например, BTCUSDT): ")
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
    users = load_users()
    if not users:
        print("Нет зарегистрированных пользователей. Создание нового:")
        return create_user()
    print("Доступные пользователи:")
    for name in users:
        print(f"- {name}")
    username = input("Выберите имя пользователя: ")
    if username not in users:
        print("Пользователь не найден.")
        return select_user()
    password = getpass("Введите пароль: ")
    if password != users[username]["password"]:
        print("Неверный пароль.")
        return select_user()
    return username

# Настройки

def default_settings():
    return {
        "profit_percentage": 0.5,
        "price_drop_percentage": 1.0,
        "delay": 30,
        "order_size": 5,
        "trading_pair": "BTCUSDT"
    }

def load_settings(user):
    settings = load_json(SETTINGS_FILE)
    return settings.get(user, default_settings())

def save_settings(user, user_settings):
    settings = load_json(SETTINGS_FILE)
    settings[user] = user_settings
    save_json(SETTINGS_FILE, settings)

def update_setting(key, value):
    user_settings = load_settings(active_user)
    user_settings[key] = value
    save_settings(active_user, user_settings)
    log(f"Настройка {key} обновлена на {value}")

# Получение цены с MEXC

def get_price(pair):
    url = f"https://api.mexc.com/api/v3/ticker/price?symbol={pair}"
    try:
        res = requests.get(url)
        return float(res.json()["price"])
    except Exception as e:
        log(f"Ошибка получения цены: {e}")
        return None

# Торговля

def autobuy_loop():
    global bot_running, bot_paused, pnl_data
    settings = load_settings(active_user)
    pair = settings["trading_pair"]
    order_size = settings["order_size"]
    profit = settings["profit_percentage"] / 100
    drop = settings["price_drop_percentage"] / 100
    delay = settings["delay"]

    price_levels = []

    while bot_running:
        if bot_paused:
            time.sleep(1)
            continue

        price = get_price(pair)
        if price is None:
            time.sleep(5)
            continue

        buy_price = price
        sell_price = buy_price * (1 + profit)

        log(f"Покупка по цене {buy_price:.4f}, продажа по {sell_price:.4f}")
        price_levels.append(buy_price)

        # Логика имитации продажи с прибылью
        realized_pnl = order_size * profit
        pnl_data["realized"] += realized_pnl
        pnl_data["trades"].append({"buy": buy_price, "sell": sell_price, "pnl": realized_pnl})
        log(f"Профит {realized_pnl:.2f} USD")

        time.sleep(delay)

# Команды

def manual():
    print("\nКоманды:")
    print("start        — запуск бота")
    print("stop         — остановка бота")
    print("pause        — пауза бота")
    print("autobuy      — запуск торгового цикла")
    print("set [ключ] [значение] — изменить настройку")
    print("manual       — список команд")
    print("bot stat     — показать состояние бота и PnL")
    print()

def bot_stat():
    settings = load_settings(active_user)
    print("\nТекущий пользователь:", active_user)
    print("Торговая пара:", settings["trading_pair"])
    print("PnL (реализованный):", round(pnl_data["realized"], 2), "USD")
    print("Совершено сделок:", len(pnl_data["trades"]))
    print("Бот работает:", bot_running)
    print("Бот на паузе:", bot_paused)
    print()

# Запуск

def main():
    global active_user, bot_running, bot_paused, trading_thread

    active_user = select_user()
    manual()

    while True:
        cmd = input("> ").strip().lower()

        if cmd == "start":
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
                bot_running = True
                trading_thread = threading.Thread(target=autobuy_loop)
                trading_thread.start()
                log("Автоматическая торговля запущена.")

        elif cmd.startswith("set "):
            try:
                _, key, value = cmd.split()
                if key in ["profit_percentage", "price_drop_percentage", "delay", "order_size"]:
                    update_setting(key, float(value))
                elif key == "trading_pair":
                    update_setting(key, value.upper())
            except Exception as e:
                log(f"Ошибка изменения настройки: {e}")

        elif cmd == "manual":
            manual()

        elif cmd == "bot stat":
            bot_stat()

        else:
            print("Неизвестная команда. Введите 'manual' для списка.")

if __name__ == "__main__":
    main()
