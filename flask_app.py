import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import requests
from flask import Flask, request

from custom_definitions import CUSTOM_GLOSSARY

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)

USER_GLOSSARY_PATH = Path(__file__).parent / "user_glossary.json"
PENDING_FEEDBACK_PATH = Path(__file__).parent / "pending_feedback.json"
ANON_MODE_PATH = Path(__file__).parent / "anon_mode.json"

MENU_DICTIONARY = "📖 Словарь"
MENU_ANONYMOUS = "🤫 Анонимный чат"

MAIN_KEYBOARD = {
    "keyboard": [[{"text": MENU_DICTIONARY}, {"text": MENU_ANONYMOUS}]],
    "resize_keyboard": True,
}

REGULATIONS_CONTEXT = """
Здесь будет текст регламентов компании 100FOODOF.
Пример: кассир должен приветствовать покупателя, предлагать карту лояльности, прощаться.
"""


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logging.exception("Не удалось прочитать %s", path)
        return {}


def save_json(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_anon_mode(chat_id: int) -> bool:
    modes = load_json(ANON_MODE_PATH)
    return bool(modes.get(str(chat_id)))


def set_anon_mode(chat_id: int, value: bool) -> None:
    modes = load_json(ANON_MODE_PATH)
    modes[str(chat_id)] = value
    save_json(ANON_MODE_PATH, modes)


def get_definition(term: str) -> Optional[str]:
    normalized = re.sub(r"\s+", " ", term.strip()).lower()
    user_glossary = load_json(USER_GLOSSARY_PATH)
    if normalized in user_glossary:
        return user_glossary[normalized]
    if normalized in CUSTOM_GLOSSARY:
        return CUSTOM_GLOSSARY[normalized]
    return None


def ask_gemini(question: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    prompt = (
        f"Ты помощник по регламентам компании 100FOODOF. "
        f"Отвечай только на основе регламентов ниже. "
        f"Если ответа нет — скажи что не знаешь.\n\n"
        f"РЕГЛАМЕНТЫ:\n{REGULATIONS_CONTEXT}\n\n"
        f"ВОПРОС: {question}"
    )
    response = requests.post(
        url,
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def send_message(chat_id: int, text: str, reply_markup="default") -> None:
    payload = {"chat_id": chat_id, "text": text[:4000]}
    if reply_markup == "default":
        payload["reply_markup"] = MAIN_KEYBOARD
    elif reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=15)
    except requests.RequestException:
        logging.exception("Не удалось отправить сообщение в Telegram")


def send_message_with_button(chat_id: int, text: str, button_text: str, callback_data: str) -> None:
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text[:4000],
                "reply_markup": {
                    "inline_keyboard": [[{"text": button_text, "callback_data": callback_data}]]
                },
            },
            timeout=15,
        )
    except requests.RequestException:
        logging.exception("Не удалось отправить сообщение с кнопкой в Telegram")


def answer_callback_query(callback_query_id: str, text: str) -> None:
    try:
        requests.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text, "show_alert": False},
            timeout=15,
        )
    except requests.RequestException:
        logging.exception("Не удалось ответить на нажатие кнопки")


def forward_feedback(chat_id: int, from_user_label: str, missing_term: Optional[str], message_text: str) -> None:
    if ADMIN_CHAT_ID:
        context = f"\nИскали термин: «{missing_term}»" if missing_term else ""
        send_message(
            int(ADMIN_CHAT_ID),
            f"📩 Сообщение от сотрудника ({from_user_label}){context}\n\n{message_text}",
            reply_markup=None,
        )
    else:
        logging.warning("ADMIN_CHAT_ID не задан — сообщение от сотрудника не доставлено")
    send_message(chat_id, "✅ Передано. Спасибо!")


def forward_anonymous(chat_id: int, message_text: str) -> None:
    if ADMIN_CHAT_ID:
        send_message(
            int(ADMIN_CHAT_ID),
            f"🤫 Анонимное сообщение от сотрудника:\n\n{message_text}",
            reply_markup=None,
        )
    else:
        logging.warning("ADMIN_CHAT_ID не задан — анонимное сообщение не доставлено")
    send_message(chat_id, "✅ Отправлено анонимно. Спасибо!")


def handle_add(chat_id: int, raw_text: str) -> None:
    payload = raw_text[len("/add"):].strip()
    if "=" not in payload:
        send_message(chat_id, "Чтобы добавить слово, напишите так:\n/add термин = определение")
        return
    term_part, definition_part = payload.split("=", 1)
    term = re.sub(r"\s+", " ", term_part.strip()).lower()
    definition = definition_part.strip()
    if not term or not definition:
        send_message(chat_id, "Не хватает термина или определения. Формат:\n/add термин = определение")
        return
    if len(term) > 100 or len(definition) > 1000:
        send_message(chat_id, "Термин или определение слишком длинные.")
        return
    user_glossary = load_json(USER_GLOSSARY_PATH)
    is_update = term in user_glossary or term in CUSTOM_GLOSSARY
    user_glossary[term] = definition
    save_json(USER_GLOSSARY_PATH, user_glossary)
    action = "Обновлено" if is_update else "Добавлено"
    send_message(chat_id, f"✅ {action}: «{term}» — {definition}")


def handle_feedback_command(chat_id: int, from_user_label: str, raw_text: str) -> None:
    payload = raw_text[len("/feedback"):].strip()
    if not payload:
        send_message(chat_id, "Напишите так:\n/feedback ваше сообщение")
        return
    forward_feedback(chat_id, from_user_label, None, payload)


def handle_text(chat_id: int, raw_text: str, from_user_label: str) -> None:
    term = re.sub(r"\s+", " ", raw_text.strip())

    if not term:
        return

    if term == MENU_DICTIONARY:
        set_anon_mode(chat_id, False)
        send_message(chat_id, "📖 Режим словаря включён.\n\nОтправьте слово, сокращение или короткий термин — отвечу по словарю компании.")
        return

    if term == MENU_ANONYMOUS:
        set_anon_mode(chat_id, True)
        send_message(chat_id, "🤫 Анонимный режим включён.\n\nВсё, что вы напишете дальше, будет передано в техподдержку без указания вашего имени.\n\nЧтобы вернуться к словарю — нажмите «📖 Словарь».")
        return

    pending = load_json(PENDING_FEEDBACK_PATH)
    key = str(chat_id)
    if key in pending:
        missing_term = pending.pop(key)
        save_json(PENDING_FEEDBACK_PATH, pending)
        if not term.startswith("/"):
            forward_feedback(chat_id, from_user_label, missing_term or None, term)
            return

    if term.startswith("/ask"):
        question = term[len("/ask"):].strip()
        if not question:
            send_message(chat_id, "Напишите вопрос после команды:\n/ask как оформить возврат?")
            return
        send_message(chat_id, "⏳ Думаю...")
        try:
            answer = ask_gemini(question)
            send_message(chat_id, f"🤖 {answer}")
        except Exception:
            logging.exception("Ошибка Gemini API")
            send_message(chat_id, "Не удалось получить ответ. Попробуйте позже.")
        return

    if term.startswith("/start"):
        set_anon_mode(chat_id, False)
        send_message(
            chat_id,
            "Отправьте слово, сокращение или короткий термин — отвечу по словарю компании.\n\n"
            "Чтобы добавить новое слово в словарь:\n"
            "/add термин = определение\n\n"
            "Чтобы написать в техподдержку:\n"
            "/feedback ваш текст\n\n"
            "Задать вопрос по регламентам:\n"
            "/ask ваш вопрос\n\n"
            "Кнопка «🤫 Анонимный чат» ниже — чтобы написать анонимно.\n\n"
            "Примеры:\n"
            "ртз\n"
            "цкп",
        )
        return

    if term.startswith("/add"):
        handle_add(chat_id, term)
        return

    if term.startswith("/feedback"):
        handle_feedback_command(chat_id, from_user_label, term)
        return

    if is_anon_mode(chat_id) and not term.startswith("/"):
        forward_anonymous(chat_id, term)
        return

    if len(term) > 100:
        send_message(chat_id, "Запрос слишком длинный. Отправьте короткий термин до 100 символов.")
        return

    if len(term.split()) > 8:
        send_message(chat_id, "Отправьте слово или короткий термин — не более восьми слов.")
        return

    if not re.fullmatch(r"[а-яА-ЯёЁa-zA-Z0-9\s\-]+", term):
        send_message(chat_id, "Используйте буквы, цифры, пробелы и дефис.")
        return

    definition = get_definition(term)

    if definition:
        send_message(chat_id, f"📖 {term}\n\n{definition}")
        return

    answer = (
        f"В словаре нет термина «{term}».\n"
        f"Можете добавить его сами: /add {term} = ваше определение\n"
        f"Или нажмите кнопку ниже, чтобы написать в техподдержку."
    )
    send_message_with_button(chat_id, answer, "✉️ Тех. поддержка", "missing_word")


def handle_callback(callback_query: dict) -> None:
    callback_id = callback_query.get("id", "")
    data = callback_query.get("data", "")
    message = callback_query.get("message", {}) or {}

    if data == "missing_word":
        text = message.get("text", "")
        match = re.search(r"«([^»]+)»", text)
        term = match.group(1) if match else None
        chat = message.get("chat", {}) or {}
        chat_id = chat.get("id")
        if chat_id is not None:
            pending = load_json(PENDING_FEEDBACK_PATH)
            pending[str(chat_id)] = term or ""
            save_json(PENDING_FEEDBACK_PATH, pending)
            send_message(chat_id, "✏️ Напишите одним сообщением, что хотите передать — я перешлю в техподдержку.")
        answer_callback_query(callback_id, "Жду ваше сообщение")
        return

    answer_callback_query(callback_id, "")


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}
    try:
        message = update.get("message") or update.get("edited_message")
        if message and "text" in message:
            chat_id = message["chat"]["id"]
            from_user = message.get("from", {}) or {}
            username = from_user.get("username")
            label = f"@{username}" if username else from_user.get("first_name", "сотрудник")
            handle_text(chat_id, message["text"], label)
        callback_query = update.get("callback_query")
        if callback_query:
            handle_callback(callback_query)
    except Exception:
        logging.exception("Необработанная ошибка при обработке обновления")
    return "OK", 200


@app.route("/")
def index():
    return "100FoodOfBot работает.", 200
