import sqlite3
import os
import json
import time
import requests
from datetime import datetime
from pydub import AudioSegment
import base64
import whisper
import re

from dotenv import load_dotenv

load_dotenv()

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# === Whisper модель для распознавания речи ===
whisper_model = whisper.load_model("base")

# === Инициализация базы данных ===
def init_db():
    conn = sqlite3.connect("diet_diary.db")
    cursor = conn.cursor()

    # Дневник питания
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            text TEXT,
            calories REAL,
            proteins REAL,
            fats REAL,
            carbs REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Таблица бадов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS supplements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT UNIQUE,
            description TEXT,
            calories REAL,
            proteins REAL,
            fats REAL,
            carbs REAL
        )
    """)
    conn.commit()

# === Распознавание голоса через Whisper ===
def voice_to_text_local(audio_path):
    try:
        audio = AudioSegment.from_ogg(audio_path)
        wav_path = "voice.wav"
        audio.export(wav_path, format="wav")
        result = whisper_model.transcribe(wav_path, language="ru")
        return result["text"].strip()
    except Exception as e:
        print(f"Ошибка при обработке голосового: {e}")
        return "Не удалось распознать речь"

# === Анализ текста через Qwen (OpenRouter) ===
def analyze_with_qwen(prompt, image_path=None):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    content = [{"type": "text", "text": prompt}]
    if image_path:
        with open(image_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{encoded_string}"}
        })

    data = {
        "model": "qwen/qwen-vl-max",
        "messages": [{"role": "user", "content": content}]
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        result = response.json()
        if 'choices' in result and len(result['choices']) > 0:
            return result['choices'][0]['message']['content']
        else:
            print("Ошибка от OpenRouter:", result)
            return "[Ошибка] Не удалось проанализировать еду."
    except Exception as e:
        print("Исключение при вызове OpenRouter:", str(e))
        return "[Ошибка] Нет связи с AI."

# === Извлечение калорий и БЖУ из текста ===
def extract_nutrition(text):
    def parse_value(value_str):
        match = re.search(r"(\d+\.?\d*)(?:\s*[-–]\s*(\d+\.?\d*))?", str(value_str))
        if not match:
            return 0.0
        if match.group(2):
            return (float(match.group(1)) + float(match.group(2))) / 2
        else:
            return float(match.group(1))

    text = str(text).lower()

    summary_start = re.search(r"(?:итог|общее количество|сумма)[^\d]*(\d+)", text, flags=re.DOTALL)
    if summary_start:
        text = text[summary_start.start():]

    calories = re.search(r"(калории|ккал)[^\d]*(\d+\.?\d*)", text, re.IGNORECASE)
    proteins = re.search(r"(белки|белок)[^\d]*(\d+\.?\d*)", text, re.IGNORECASE)
    fats = re.search(r"(жиры|жир)[^\d]*(\d+\.?\d*)", text, re.IGNORECASE)
    carbs = re.search(r"(углеводы|углевода)[^\d]*(\d+\.?\d*)", text, re.IGNORECASE)

    return {
        "calories": round(parse_value(calories.group(0)) if calories else 0, 1),
        "proteins": round(parse_value(proteins.group(0)) if proteins else 0, 1),
        "fats": round(parse_value(fats.group(0)) if fats else 0, 1),
        "carbs": round(parse_value(carbs.group(0)) if carbs else 0, 1)
    }

# === Отправка сообщения с кнопками ===
def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    response = requests.post(f"{API_URL}/sendMessage", data=payload)
    return response.json() if response.status_code == 200 else None

# === Скачивание файла от Telegram ===
def download_file(file_path, filename):
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    response = requests.get(url)
    with open(filename, "wb") as f:
        f.write(response.content)

# === Сохранение записи в дневник ===
def save_entry(user_id, text, calories, proteins=0, fats=0, carbs=0):
    conn = sqlite3.connect("diet_diary.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO entries (user_id, text, calories, proteins, fats, carbs) VALUES (?, ?, ?, ?, ?, ?)",
                   (user_id, text, calories, proteins, fats, carbs))
    conn.commit()

# === Получение записей пользователя за день ===
def get_entries_today(user_id):
    conn = sqlite3.connect("diet_diary.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, text, calories, proteins, fats, carbs
        FROM entries
        WHERE user_id = ? AND DATE(timestamp) = DATE('now')
    """, (user_id,))
    return cursor.fetchall()

# === Получение всех бадов пользователя ===
def get_all_supplements(user_id):
    conn = sqlite3.connect("diet_diary.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM supplements WHERE user_id = ?", (user_id,))
    return [row[0] for row in cursor.fetchall()]

# === Сохранение бада в справочнике ===
def save_supplement(user_id, name, description, calories, proteins, fats, carbs):
    conn = sqlite3.connect("diet_diary.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO supplements
        (user_id, name, description, calories, proteins, fats, carbs)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, name, description, calories, proteins, fats, carbs))
    conn.commit()

# === Получение бада по имени ===
def get_supplement(user_id, name):
    conn = sqlite3.connect("diet_diary.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM supplements WHERE user_id = ? AND name = ?", (user_id, name))
    return cursor.fetchone()

# === Обработка входящего сообщения ===
current_analysis = {}  # Хранилище анализа перед подтверждением
user_states = {}       # Для пошагового диалога

def handle_message(update):
    message = update.get("message", {})
    chat_id = message["chat"]["id"]
    from_id = message["from"]["id"]
    text = message.get("text")
    voice = message.get("voice")
    photo = message.get("photo")

    # Голосовое сообщение
    if voice:
        file_id = voice["file_id"]
        file_info = requests.get(f"{API_URL}/getFile?file_id={file_id}").json()
        file_path = file_info["result"]["file_path"]

        download_path = "user_voice.ogg"
        download_file(file_path, download_path)

        raw_text = voice_to_text_local(download_path)
        analysis = analyze_with_qwen(f"Что съел пользователь и сколько калорий и БЖУ? '{raw_text}'")
        nutrients = extract_nutrition(analysis)

        current_analysis[chat_id] = {
            "user_id": from_id,
            "text": raw_text,
            "calories": nutrients["calories"],
            "proteins": nutrients["proteins"],
            "fats": nutrients["fats"],
            "carbs": nutrients["carbs"]
        }
        buttons = {
            "inline_keyboard": [
                [{"text": "✅ Сохранить", "callback_data": "save_yes"},
                 {"text": "❌ Удалить", "callback_data": "save_no"}]
            ]
        }
        msg = send_message(chat_id, f"Вы сказали: '{raw_text}'\n\nКалории: {nutrients['calories']} ккал\nБелки: {nutrients['proteins']} г\nЖиры: {nutrients['fats']} г\nУглеводы: {nutrients['carbs']} г\n\nСохранить в дневнике?", buttons)

        if msg and "result" in msg:
            current_analysis[chat_id]["message_id"] = msg["result"]["message_id"]

    # Фото
    elif photo:
        file_id = photo[-1]["file_id"]
        file_info = requests.get(f"{API_URL}/getFile?file_id={file_id}").json()
        file_path = file_info["result"]["file_path"]

        download_path = "user_food.jpg"
        download_file(file_path, download_path)

        analysis = analyze_with_qwen("Опиши продукты на фото. Рассчитай суммарные калории и БЖУ.", image_path=download_path)
        nutrients = extract_nutrition(analysis)

        current_analysis[chat_id] = {
            "user_id": from_id,
            "text": analysis,
            "calories": nutrients["calories"],
            "proteins": nutrients["proteins"],
            "fats": nutrients["fats"],
            "carbs": nutrients["carbs"]
        }
        buttons = {
            "inline_keyboard": [
                [{"text": "✅ Сохранить", "callback_data": "save_yes"},
                 {"text": "❌ Удалить", "callback_data": "save_no"}]
            ]
        }
        msg = send_message(chat_id, f"На фото: {analysis}\n\nКалории: {nutrients['calories']} ккал\nБелки: {nutrients['proteins']} г\nЖиры: {nutrients['fats']} г\nУглеводы: {nutrients['carbs']} г\n\nЗапомнить как съеденное?", buttons)

        if msg and "result" in msg:
            current_analysis[chat_id]["message_id"] = msg["result"]["message_id"]

    # Текстовое сообщение
    elif text:
        if text == "/start":
            start_msg = """
👋 Привет! Я помогу тебе считать калории и БЖУ.

📸 Пришли фото еды
🎙 Запиши голосом, что ты ел
📝 Или просто напиши мне

А ещё я могу запомнить твои любимые блюда и бады!
"""
            send_message(chat_id, start_msg.strip())
            show_main_menu(chat_id)

        elif text.startswith("/add_supplement"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                send_message(chat_id, "❌ Укажите название бада после команды.\nПример: /add_supplement Витамин D3")
                return

            _, name = parts
            analysis = analyze_with_qwen(f"Опиши состав этого бада: '{name}'. Сколько калорий, белков, жиров, углеводов?")
            nutrients = extract_nutrition(analysis)
            save_supplement(from_id, name, analysis, nutrients["calories"], nutrients["proteins"], nutrients["fats"], nutrients["carbs"])
            send_message(chat_id, f"✅ Бад '{name}' добавлен:\n{analysis}")

        elif text.startswith("/take"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                supplements = get_all_supplements(from_id)
                if not supplements:
                    send_message(chat_id, "❌ У вас пока нет сохранённых бадов.")
                    return

                buttons = {"inline_keyboard": []}
                for name in supplements:
                    buttons["inline_keyboard"].append([{
                        "text": name,
                        "callback_data": f"take_{name}"
                    }])
                buttons["inline_keyboard"].append([{"text": "⬅️ Назад", "callback_data": "back"}])
                send_message(chat_id, "Выберите бад для приёма:", buttons)
                return

            _, name = parts
            supplement = get_supplement(from_id, name)
            if supplement:
                save_entry(from_id, f"Принят бад: {name}", supplement[3], supplement[4], supplement[5], supplement[6])
                send_message(chat_id, f"✅ Вы приняли '{name}'. Записано в дневник.")
            else:
                send_message(chat_id, f"❌ Бад '{name}' не найден.")

        elif text == "/stats":
            entries = get_entries_today(from_id)
            if not entries:
                send_message(chat_id, "📊 Ваш дневник питания пуст сегодня.")
                return

            cal, prot, fat, carb = 0.0, 0.0, 0.0, 0.0
            buttons = {"inline_keyboard": []}
            for entry in entries:
                cal += float(entry[2] or 0)
                prot += float(entry[3] or 0)
                fat += float(entry[4] or 0)
                carb += float(entry[5] or 0)
                buttons["inline_keyboard"].append([
                    {"text": f"🗑️ Удалить #{entry[0]}", "callback_data": f"delete_{entry[0]}"}
                ])

            buttons["inline_keyboard"].append([{"text": "⬅️ Назад", "callback_data": "back"}])
            send_message(chat_id, f"""
📊 Ваша статистика за сегодня:
🔥 Калории: {cal:.1f} ккал
🍗 Белки: {prot:.1f} г
🥑 Жиры: {fat:.1f} г
🍞 Углеводы: {carb:.1f} г
""", buttons)

        else:
            portion_match = re.search(r"(\d+)\s*(грамм|г)", text, re.IGNORECASE)
            grams = int(portion_match.group(1)) if portion_match else 100
            scale = grams / 100

            analysis = analyze_with_qwen(f"Что съел пользователь и сколько калорий и БЖУ? '{text}'")
            nutrients = extract_nutrition(analysis)

            scaled_nutrients = {
                "calories": nutrients["calories"] * scale,
                "proteins": nutrients["proteins"] * scale,
                "fats": nutrients["fats"] * scale,
                "carbs": nutrients["carbs"] * scale
            }

            current_analysis[chat_id] = {
                "user_id": from_id,
                "text": text,
                "calories": scaled_nutrients["calories"],
                "proteins": scaled_nutrients["proteins"],
                "fats": scaled_nutrients["fats"],
                "carbs": scaled_nutrients["carbs"]
            }

            buttons = {
                "inline_keyboard": [
                    [{"text": "✅ Сохранить", "callback_data": "save_yes"},
                     {"text": "❌ Удалить", "callback_data": "save_no"}]
                ]
            }

            send_message(chat_id, f"Вы написали: '{text}'\n\nКалории: {scaled_nutrients['calories']:.1f} ккал\nБелки: {scaled_nutrients['proteins']:.1f} г\nЖиры: {scaled_nutrients['fats']:.1f} г\nУглеводы: {scaled_nutrients['carbs']:.1f} г\n\nСохранить в дневнике?", buttons)

# === Главное меню с командами ===
def show_main_menu(chat_id):
    buttons = {
        "keyboard": [
            ["/start"],
            ["/stats"],
            ["/add_supplement", "/take"]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }
    payload = {
        "chat_id": chat_id,
        "text": "Выберите команду:",
        "reply_markup": json.dumps(buttons)
    }
    requests.post(f"{API_URL}/sendMessage", data=payload)

# === Обработка нажатия кнопок ===
processed_callbacks = set()  # Избегаем повторной обработки

def handle_callback(update):
    callback = update.get("callback_query", {})
    chat_id = callback["message"]["chat"]["id"]
    message_id = callback["message"]["message_id"]
    data = callback["data"]
    callback_id = callback.get("id")

    if callback_id in processed_callbacks:
        return
    processed_callbacks.add(callback_id)

    if data == "save_yes" and chat_id in current_analysis:
        entry = current_analysis.pop(chat_id)
        save_entry(entry["user_id"], entry["text"], entry["calories"], entry["proteins"], entry["fats"], entry["carbs"])
        send_message(chat_id, f"✅ Добавлено в дневник: {entry['calories']:.1f} ккал")

    elif data == "save_no" and chat_id in current_analysis:
        current_analysis.pop(chat_id)
        delete_message(chat_id, message_id)
        send_message(chat_id, "❌ Сообщение удалено.")

    elif data.startswith("delete_"):
        entry_id = int(data.replace("delete_", ""))
        conn = sqlite3.connect("diet_diary.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        conn.commit()
        send_message(chat_id, f"🗑️ Запись #{entry_id} удалена из дневника.")

    elif data.startswith("take_"):
        supplement_name = data.replace("take_", "")
        supplement = get_supplement(chat_id, supplement_name)
        if supplement:
            save_entry(chat_id, f"Принят бад: {supplement_name}",
                      supplement[3], supplement[4], supplement[5], supplement[6])
            send_message(chat_id, f"✅ Вы приняли '{supplement_name}'. Записано в дневник.")
        else:
            send_message(chat_id, f"❌ Бад '{supplement_name}' не найден.")

    elif data == "back":
        show_main_menu(chat_id)

# === Удаление сообщения по ID ===
def delete_message(chat_id, message_id):
    payload = {"chat_id": chat_id, "message_id": message_id}
    response = requests.post(f"{API_URL}/deleteMessage", data=payload)
    return response.status_code == 200

# === Получение обновлений от Telegram ===
def get_updates(offset=None):
    params = {"timeout": 100}
    if offset is not None:
        params["offset"] = offset
    response = requests.get(f"{API_URL}/getUpdates", params=params)
    return response.json().get("result", [])

# === Основной цикл ===
def main():
    init_db()
    last_update_id = None

    while True:
        updates = get_updates(last_update_id)
        for update in updates:
            if "message" in update:
                handle_message(update)
                last_update_id = update["update_id"] + 1
            elif "callback_query" in update:
                handle_callback(update)

        time.sleep(1)

if __name__ == "__main__":
    main()
