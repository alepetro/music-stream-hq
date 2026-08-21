#!/usr/bin/env python3
"""
Studio-quality multi-language Telegram Music Bot
Fixed version: YouTube cookies + bot-detection bypass.
"""

import base64
import functools
import glob
import html
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import tempfile
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests
import telebot
from telebot import types
import yt_dlp
from ytmusicapi import YTMusic

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID = 8584283379
DB_PATH = os.path.join(os.path.dirname(__file__), "bot_data.db")
PAGE_SIZE = 10
SEARCH_POOL = 40

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=True, num_threads=32)

user_search_cache: dict[int, dict] = {}
lang_cache: dict[int, str] = {}

BTN_PREV, BTN_CLOSE, BTN_NEXT = "⬅️", "❌", "➡️"

# ---------------------------------------------------------------------------
# YouTube extraction (2026)
# ---------------------------------------------------------------------------
# YouTube blocks unauthenticated server IPs.  Two complementary fixes:
#
# 1. NODE PATH — yt-dlp-ejs uses Node.js to solve YouTube's JS player
#    challenges.  In Replit deployments Node may live in the Nix store and
#    not be on PATH by default; we detect it at startup and add it.
#
# 2. COOKIES — authenticated requests bypass bot-detection entirely.
#    Set the YOUTUBE_COOKIES secret (Netscape cookies.txt format, optionally
#    base64-encoded) and the bot will use it automatically.

def _setup_node_path() -> None:
    """Ensure Node.js is in PATH so yt-dlp-ejs can solve JS challenges."""
    if shutil.which("node"):
        log.info("Node.js already in PATH: %s", shutil.which("node"))
        return
    # Search Nix store (Replit) and standard Linux paths
    candidates = (
        sorted(glob.glob("/nix/store/*nodejs*wrapped*/bin/node"))
        + sorted(glob.glob("/nix/store/*nodejs*/bin/node"))
        + ["/usr/local/bin/node", "/usr/bin/node", "/opt/nodejs/bin/node"]
    )
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            node_dir = os.path.dirname(candidate)
            os.environ["PATH"] = node_dir + ":" + os.environ.get("PATH", "")
            log.info("Added Node.js to PATH from Nix store: %s", candidate)
            return
    log.warning("Node.js not found — YouTube JS challenge solving may be unavailable.")


_YT_COOKIES_FILE: str | None = None

def _setup_yt_cookies() -> None:
    """Write YOUTUBE_COOKIES env var to a temp file once at startup."""
    global _YT_COOKIES_FILE
    raw = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not raw:
        local = os.path.join(os.path.dirname(__file__), "youtube_cookies.txt")
        if os.path.isfile(local):
            _YT_COOKIES_FILE = local
            log.info("YouTube cookies loaded from youtube_cookies.txt")
        else:
            log.warning("YOUTUBE_COOKIES secret not set — downloads may fail on server IPs.")
        return
    # Decode base64 if necessary
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
        if decoded.startswith("# Netscape") or "\tyoutube.com\t" in decoded or ".youtube.com\t" in decoded:
            raw = decoded
    except Exception:
        pass
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix="_yt_cookies.txt",
                                      delete=False, prefix="musicbot_")
    tmp.write(raw)
    tmp.close()
    _YT_COOKIES_FILE = tmp.name
    log.info("YouTube cookies written to %s", _YT_COOKIES_FILE)

# Clients to try for audio extraction.
# With cookies the web client is reliable; embedded clients are the best
# unauthenticated option since they are less aggressively rate-limited.
YDL_EXTRACTOR_ARGS = {
    "extractor_args": {
        "youtube": {
            "player_client": ["web", "web_embedded", "mweb", "ios"],
            "player_skip": [],
        }
    }
}

YDL_JS_RUNTIMES = {"js_runtimes": {"node": {}}}

# ---------------------------------------------------------------------------
# Anti-flood, bounded concurrency & auto-cleaner
# ---------------------------------------------------------------------------

download_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="dl")
active_downloads: set[int] = set()
_active_lock = threading.Lock()

_flood: dict[int, list[float]] = {}
_flood_lock = threading.Lock()
FLOOD_MIN_INTERVAL = 1.0
FLOOD_MAX_PER_MIN = 20

_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00ad\x00-\x08\x0b\x0c\x0e-\x1f]")


def flood_ok(uid: int) -> bool:
    now = time.monotonic()
    with _flood_lock:
        stamps = _flood.setdefault(uid, [])
        while stamps and now - stamps[0] > 60:
            stamps.pop(0)
        if stamps and now - stamps[-1] < FLOOD_MIN_INTERVAL:
            return False
        if len(stamps) >= FLOOD_MAX_PER_MIN:
            return False
        stamps.append(now)
        return True


_cmd_dedupe: dict[int, dict[str, float]] = {}
_cmd_dedupe_lock = threading.Lock()
CMD_DEDUPE_WINDOW = 4.0
STALE_MESSAGE_AGE = 45


def message_fresh(message: types.Message) -> bool:
    try:
        return not (message.date and time.time() - message.date > STALE_MESSAGE_AGE)
    except Exception:
        return True


def command_ok(message: types.Message) -> bool:
    try:
        if message.date and time.time() - message.date > STALE_MESSAGE_AGE:
            return False
        cmd = " ".join((message.text or "").lower().split())
        uid = message.from_user.id
        now = time.monotonic()
        with _cmd_dedupe_lock:
            per_user = _cmd_dedupe.setdefault(uid, {})
            last = per_user.get(cmd, 0.0)
            if now - last < CMD_DEDUPE_WINDOW:
                return False
            per_user[cmd] = now
        return True
    except Exception:
        return True


def esc(text: str) -> str:
    return html.escape(_INVISIBLE_RE.sub("", text or "").strip())


def _auto_cleaner() -> None:
    while True:
        time.sleep(600)
        try:
            now = time.time()
            for uid in list(user_search_cache):
                state = user_search_cache.get(uid)
                if state and now - state.get("ts", now) > 7200:
                    user_search_cache.pop(uid, None)
            if len(lang_cache) > 5000:
                lang_cache.clear()
            with _search_cache_lock:
                for key in list(_search_result_cache):
                    entry = _search_result_cache.get(key)
                    if entry and now - entry["ts"] > SEARCH_CACHE_TTL:
                        _search_result_cache.pop(key, None)
            with _flood_lock:
                for uid in list(_flood):
                    if not _flood[uid] or time.monotonic() - _flood[uid][-1] > 300:
                        _flood.pop(uid, None)
            tmp_root = tempfile.gettempdir()
            for entry in Path(tmp_root).glob("musicbot_*"):
                try:
                    if entry.is_dir() and now - entry.stat().st_mtime > 3600:
                        shutil.rmtree(entry, ignore_errors=True)
                except Exception:
                    pass
            try:
                conn = get_db()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                conn.close()
            except Exception:
                pass
        except Exception as exc:
            log.warning("Auto-cleaner iteration failed: %s", exc)

# ---------------------------------------------------------------------------
# Multi-language strings
# ---------------------------------------------------------------------------

LANGUAGES: dict[str, dict[str, str]] = {
    "it": {
        "searching": "🔍 Cerco tra i siti di streaming musicale...",
        "downloading": "⬇️ Scarico la traccia...",
        "sending": "📤 Invio in corso...",
        "not_found": "❌ Nessun risultato trovato.",
        "too_long": "❌ Traccia troppo lunga (max 10 min).",
        "lyrics_not_found": "❌ Testo non trovato.",
        "choose_lang": "🌍 Scegli la tua lingua:",
        "lang_set": "✅ Lingua impostata su Italiano 🇮🇹",
        "invalid": "⚠️ Numero non valido.",
        "no_search": "⚠️ Prima cerca una canzone.",
        "cached": "⚡ Invio dalla cache...",
        "welcome": "👋 <b>Benvenuto su Music Stream HQ!</b> 🎧\n<i>Il tuo hub personale per cercare, riconoscere e scaricare musica ad altissima fedeltà.</i>\n\n━━━━━━━━━━━━━━\n🆓 <b>PIANO GRATUITO (FREE):</b>\n• 🎧 <b>Shazam Integrato:</b> Invia un video TikTok, un Reel o un vocale per scoprire al volo il titolo della canzone! 🔍\n• 🎵 <b>Download ILLIMITATI e gratuiti</b> (MP3 320kbps in alta qualità)\n• 🔍 <b>Ricerca Intelligente:</b> Cerca per Titolo, Artista o Link (YouTube, Spotify, SoundCloud)\n• 📜 <b>Testi delle canzoni</b> (<i>Lyrics</i>) gratuiti e sbloccati per tutti\n• ✈️ <b>Ascolto Offline:</b> Salva le canzoni e ascoltale anche in Modalità Aereo!\n\n⭐ <b>FUNZIONALITÀ PREMIUM (SBLOCCABILI):</b>\n• 📂 <b>Download Playlist &amp; Album</b> — Scarica raccolte intere in 1 solo click\n• 💎 <b>Qualità FLAC / Lossless</b> — Audio puro da studio senza compressione\n• 🎤 <b>Karaoke / Vocal Remover</b> — Separa ed elimina la voce dalla base\n• 🌀 <b>Audio 8D &amp; Effetti FX</b> — Suono spaziale a 360°, Bass Boost &amp; EXTREME, Nightcore, Speed Up, Slowed+Reverb, Lo-Fi, Live Concert\n• ⚡ <b>Coda Prioritaria</b> — Download ultra-veloci sui server\n\n━━━━━━━━━━━━━━\n🚀 <i>Pronto ad iniziare? Invia subito un video TikTok, un vocale o scrivi il nome di un brano qui in chat!</i> 👇",
        "offline_help": "✈️ Salva i brani sul telefono dalla chat: una volta scaricati li ascolti anche senza internet o in Modalità Aereo!",
        "btn_go_premium": "⭐ Passa a Premium (Stelle)",
        "btn_offline": "✈️ Come ascoltare Offline?",
        "btn_lang": "🌍 Cambia Lingua",
        "admin_header": "📊 <b>Pannello Admin</b>",
        "admin_denied": "⛔ Accesso negato.",
        "url_processing": "🔗 Processo il link...",
        "closed": "✅ Ricerca chiusa.",
        "cmd_start": "Avvia il bot",
        "cmd_language": "Cambia lingua",
        "cmd_help": "Guida",
    },
    "en": {
        "searching": "🔍 Searching music streaming sites...",
        "downloading": "⬇️ Downloading track...",
        "sending": "📤 Sending...",
        "not_found": "❌ No results found.",
        "too_long": "❌ Track too long (max 10 min).",
        "lyrics_not_found": "❌ Lyrics not found.",
        "choose_lang": "🌍 Choose your language:",
        "lang_set": "✅ Language set to English 🇬🇧",
        "invalid": "⚠️ Invalid number.",
        "no_search": "⚠️ Search for a song first.",
        "cached": "⚡ Sending from cache...",
        "welcome": "👋 <b>Welcome to Music Stream HQ!</b> 🎧\n<i>Your personal hub to search, recognize, and download music in the highest quality.</i>\n\n━━━━━━━━━━━━━━\n🆓 <b>FREE PLAN:</b>\n• 🎧 <b>Built-in Shazam:</b> Send a TikTok video, a Reel, or a voice note to instantly discover the song! 🔍\n• 🎵 <b>UNLIMITED free downloads</b> (MP3 320kbps high quality)\n• 🔍 <b>Smart Search:</b> Search by title, artist or link (YouTube, Spotify, SoundCloud)\n• 📜 <b>Song Lyrics</b> — free and unlocked for everyone\n• ✈️ <b>Offline Listening:</b> Save songs and listen even in Airplane Mode!\n\n⭐ <b>PREMIUM FEATURES (UNLOCKABLE):</b>\n• 📂 <b>Playlist &amp; Album Downloads</b> — Full collections in 1 click\n• 💎 <b>FLAC / Lossless Quality</b> — Pure studio audio, no compression\n• 🎤 <b>Karaoke / Vocal Remover</b> — Separate and remove vocals from the beat\n• 🌀 <b>8D Audio &amp; FX Effects</b> — 360° spatial sound, Bass Boost &amp; EXTREME, Nightcore, Speed Up, Slowed+Reverb, Lo-Fi, Live Concert\n• ⚡ <b>Priority Queue</b> — Ultra-fast downloads on our servers\n\n━━━━━━━━━━━━━━\n🚀 <i>Ready to start? Send a TikTok video, a voice note, or type a song name in chat right now!</i> 👇",
        "offline_help": "✈️ Save tracks to your phone from this chat: once downloaded you can listen with no internet or in Airplane Mode!",
        "btn_go_premium": "⭐ Go Premium (Stars)",
        "btn_offline": "✈️ How to listen Offline?",
        "btn_lang": "🌍 Change Language",
        "admin_header": "📊 <b>Admin Panel</b>",
        "admin_denied": "⛔ Access denied.",
        "url_processing": "🔗 Processing link...",
        "closed": "✅ Search closed.",
        "cmd_start": "Start the bot",
        "cmd_language": "Change language",
        "cmd_help": "Help",
    },
    "es": {
        "searching": "🔍 Buscando en los sitios de streaming musical...",
        "downloading": "⬇️ Descargando pista...",
        "sending": "📤 Enviando...",
        "not_found": "❌ No se encontraron resultados.",
        "too_long": "❌ Pista demasiado larga (máx. 10 min).",
        "lyrics_not_found": "❌ Letra no encontrada.",
        "choose_lang": "🌍 Elige tu idioma:",
        "lang_set": "✅ Idioma configurado a Español 🇪🇸",
        "invalid": "⚠️ Número inválido.",
        "no_search": "⚠️ Primero busca una canción.",
        "cached": "⚡ Enviando desde caché...",
        "welcome": "👋 <b>¡Bienvenido a Music Stream HQ!</b> 🎧",
        "offline_help": "✈️ Guarda las pistas en tu teléfono desde este chat.",
        "btn_go_premium": "⭐ Hazte Premium (Estrellas)",
        "btn_offline": "✈️ ¿Cómo escuchar Offline?",
        "btn_lang": "🌍 Cambiar Idioma",
        "admin_header": "📊 <b>Panel Admin</b>",
        "admin_denied": "⛔ Acceso denegado.",
        "url_processing": "🔗 Procesando enlace...",
        "closed": "✅ Búsqueda cerrada.",
        "cmd_start": "Iniciar el bot",
        "cmd_language": "Cambiar idioma",
        "cmd_help": "Ayuda",
    },
    "fr": {
        "searching": "🔍 Recherche sur les sites de streaming musical...",
        "downloading": "⬇️ Téléchargement...",
        "sending": "📤 Envoi...",
        "not_found": "❌ Aucun résultat trouvé.",
        "too_long": "❌ Piste trop longue (max 10 min).",
        "lyrics_not_found": "❌ Paroles introuvables.",
        "choose_lang": "🌍 Choisissez votre langue:",
        "lang_set": "✅ Langue définie sur Français 🇫🇷",
        "invalid": "⚠️ Numéro invalide.",
        "no_search": "⚠️ Cherchez d'abord une chanson.",
        "cached": "⚡ Envoi depuis le cache...",
        "welcome": "👋 <b>Bienvenue sur Music Stream HQ !</b> 🎧",
        "offline_help": "✈️ Enregistrez les titres sur votre téléphone depuis ce chat.",
        "btn_go_premium": "⭐ Passer Premium (Étoiles)",
        "btn_offline": "✈️ Écouter hors ligne ?",
        "btn_lang": "🌍 Changer de langue",
        "admin_header": "📊 <b>Panneau Admin</b>",
        "admin_denied": "⛔ Accès refusé.",
        "url_processing": "🔗 Traitement du lien...",
        "closed": "✅ Recherche fermée.",
        "cmd_start": "Démarrer le bot",
        "cmd_language": "Changer de langue",
        "cmd_help": "Aide",
    },
    "de": {
        "searching": "🔍 Musik-Streaming-Seiten werden durchsucht...",
        "downloading": "⬇️ Herunterladen...",
        "sending": "📤 Senden...",
        "not_found": "❌ Keine Ergebnisse gefunden.",
        "too_long": "❌ Titel zu lang (max. 10 Min.).",
        "lyrics_not_found": "❌ Songtext nicht gefunden.",
        "choose_lang": "🌍 Wählen Sie Ihre Sprache:",
        "lang_set": "✅ Sprache auf Deutsch 🇩🇪 eingestellt",
        "invalid": "⚠️ Ungültige Nummer.",
        "no_search": "⚠️ Suchen Sie zuerst einen Song.",
        "cached": "⚡ Aus Cache senden...",
        "welcome": "👋 <b>Willkommen bei Music Stream HQ!</b> 🎧",
        "offline_help": "✈️ Speichere Songs aus diesem Chat auf deinem Handy.",
        "btn_go_premium": "⭐ Premium holen (Sterne)",
        "btn_offline": "✈️ Offline hören – wie?",
        "btn_lang": "🌍 Sprache ändern",
        "admin_header": "📊 <b>Admin-Panel</b>",
        "admin_denied": "⛔ Zugriff verweigert.",
        "url_processing": "🔗 Link wird verarbeitet...",
        "closed": "✅ Suche geschlossen.",
        "cmd_start": "Bot starten",
        "cmd_language": "Sprache ändern",
        "cmd_help": "Hilfe",
    },
    "pt": {
        "searching": "🔍 Pesquisando nos sites de streaming de música...",
        "downloading": "⬇️ Baixando faixa...",
        "sending": "📤 Enviando...",
        "not_found": "❌ Nenhum resultado encontrado.",
        "too_long": "❌ Faixa muito longa (máx. 10 min).",
        "lyrics_not_found": "❌ Letra não encontrada.",
        "choose_lang": "🌍 Escolha seu idioma:",
        "lang_set": "✅ Idioma definido para Português 🇵🇹",
        "invalid": "⚠️ Número inválido.",
        "no_search": "⚠️ Procure uma música primeiro.",
        "cached": "⚡ Enviando do cache...",
        "welcome": "👋 <b>Bem-vindo ao Music Stream HQ!</b> 🎧",
        "offline_help": "✈️ Salve as faixas no celular a partir deste chat.",
        "btn_go_premium": "⭐ Seja Premium (Estrelas)",
        "btn_offline": "✈️ Como ouvir Offline?",
        "btn_lang": "🌍 Mudar Idioma",
        "admin_header": "📊 <b>Painel Admin</b>",
        "admin_denied": "⛔ Acesso negado.",
        "url_processing": "🔗 Processando link...",
        "closed": "✅ Pesquisa fechada.",
        "cmd_start": "Iniciar o bot",
        "cmd_language": "Mudar idioma",
        "cmd_help": "Ajuda",
    },
    "ru": {
        "searching": "🔍 Ищу на музыкальных стриминговых сервисах...",
        "downloading": "⬇️ Скачивание...",
        "sending": "📤 Отправка...",
        "not_found": "❌ Результаты не найдены.",
        "too_long": "❌ Трек слишком длинный (макс. 10 мин).",
        "lyrics_not_found": "❌ Текст не найден.",
        "choose_lang": "🌍 Выберите язык:",
        "lang_set": "✅ Язык установлен на Русский 🇷🇺",
        "invalid": "⚠️ Неверный номер.",
        "no_search": "⚠️ Сначала найдите песню.",
        "cached": "⚡ Отправка из кэша...",
        "welcome": "👋 <b>Добро пожаловать в Music Stream HQ!</b> 🎧",
        "offline_help": "✈️ Сохраняйте треки на телефон из этого чата.",
        "btn_go_premium": "⭐ Премиум (Звёзды)",
        "btn_offline": "✈️ Как слушать офлайн?",
        "btn_lang": "🌍 Сменить язык",
        "admin_header": "📊 <b>Панель администратора</b>",
        "admin_denied": "⛔ Доступ запрещён.",
        "url_processing": "🔗 Обработка ссылки...",
        "closed": "✅ Поиск закрыт.",
        "cmd_start": "Запустить бота",
        "cmd_language": "Сменить язык",
        "cmd_help": "Помощь",
    },
    "uz": {
        "searching": "🔍 Musiqa striming saytlarida qidirilmoqda...",
        "downloading": "⬇️ Yuklanmoqda...",
        "sending": "📤 Yuborilmoqda...",
        "not_found": "❌ Natija topilmadi.",
        "too_long": "❌ Trek juda uzun (maks. 10 daqiqa).",
        "lyrics_not_found": "❌ Qo'shiq matni topilmadi.",
        "choose_lang": "🌍 Tilni tanlang:",
        "lang_set": "✅ Til O'zbek 🇺🇿 ga o'rnatildi",
        "invalid": "⚠️ Noto'g'ri raqam.",
        "no_search": "⚠️ Avval qo'shiq qidiring.",
        "cached": "⚡ Keshdan yuborilmoqda...",
        "welcome": "👋 <b>Music Stream HQ ga xush kelibsiz!</b> 🎧",
        "offline_help": "✈️ Treklarni shu chatdan telefoningizga saqlang.",
        "btn_go_premium": "⭐ Premium olish (Yulduzlar)",
        "btn_offline": "✈️ Oflayn qanday tinglanadi?",
        "btn_lang": "🌍 Tilni o'zgartirish",
        "admin_header": "📊 <b>Admin Panel</b>",
        "admin_denied": "⛔ Ruxsat yo'q.",
        "url_processing": "🔗 Havola qayta ishlanmoqda...",
        "closed": "✅ Qidiruv yopildi.",
        "cmd_start": "Botni ishga tushirish",
        "cmd_language": "Tilni o'zgartirish",
        "cmd_help": "Yordam",
    },
    "hi": {
        "searching": "🔍 म्यूज़िक स्ट्रीमिंग साइटों पर खोज रहे हैं...",
        "downloading": "⬇️ डाउनलोड हो रहा है...",
        "sending": "📤 भेज रहे हैं...",
        "not_found": "❌ कोई परिणाम नहीं मिला।",
        "too_long": "❌ ट्रैक बहुत लंबा है (अधिकतम 10 मिनट)।",
        "lyrics_not_found": "❌ बोल नहीं मिले।",
        "choose_lang": "🌍 अपनी भाषा चुनें:",
        "lang_set": "✅ भाषा हिन्दी 🇮🇳 पर सेट की गई",
        "invalid": "⚠️ अमान्य नंबर।",
        "no_search": "⚠️ पहले एक गाना खोजें।",
        "cached": "⚡ कैश से भेज रहे हैं...",
        "welcome": "👋 <b>Music Stream HQ में आपका स्वागत है!</b> 🎧",
        "offline_help": "✈️ इस चैट से गाने अपने फ़ोन में सेव करें।",
        "btn_go_premium": "⭐ प्रीमियम लें (स्टार्स)",
        "btn_offline": "✈️ ऑफ़लाइन कैसे सुनें?",
        "btn_lang": "🌍 भाषा बदलें",
        "admin_header": "📊 <b>एडमिन पैनल</b>",
        "admin_denied": "⛔ पहुँच अस्वीकृत।",
        "url_processing": "🔗 लिंक प्रोसेस हो रहा है...",
        "closed": "✅ खोज बंद।",
        "cmd_start": "बॉट शुरू करें",
        "cmd_language": "भाषा बदलें",
        "cmd_help": "मदद",
    },
    "zh": {
        "searching": "🔍 正在搜索音乐流媒体网站...",
        "downloading": "⬇️ 下载中...",
        "sending": "📤 发送中...",
        "not_found": "❌ 未找到结果。",
        "too_long": "❌ 曲目太长（最长10分钟）。",
        "lyrics_not_found": "❌ 未找到歌词。",
        "choose_lang": "🌍 选择您的语言:",
        "lang_set": "✅ 语言已设置为中文 🇨🇳",
        "invalid": "⚠️ 无效号码。",
        "no_search": "⚠️ 请先搜索一首歌。",
        "cached": "⚡ 从缓存发送...",
        "welcome": "👋 <b>欢迎使用 Music Stream HQ！</b> 🎧",
        "offline_help": "✈️ 从此聊天将歌曲保存到手机。",
        "btn_go_premium": "⭐ 升级高级版（星星）",
        "btn_offline": "✈️ 如何离线收听？",
        "btn_lang": "🌍 更换语言",
        "admin_header": "📊 <b>管理员面板</b>",
        "admin_denied": "⛔ 拒绝访问。",
        "url_processing": "🔗 处理链接中...",
        "closed": "✅ 搜索已关闭。",
        "cmd_start": "启动机器人",
        "cmd_language": "更改语言",
        "cmd_help": "帮助",
    },
    "ja": {
        "searching": "🔍 音楽ストリーミングサイトを検索中...",
        "downloading": "⬇️ ダウンロード中...",
        "sending": "📤 送信中...",
        "not_found": "❌ 結果が見つかりません。",
        "too_long": "❌ トラックが長すぎます（最大10分）。",
        "lyrics_not_found": "❌ 歌詞が見つかりません。",
        "choose_lang": "🌍 言語を選択してください:",
        "lang_set": "✅ 言語が日本語 🇯🇵 に設定されました",
        "invalid": "⚠️ 無効な番号です。",
        "no_search": "⚠️ まず曲を検索してください。",
        "cached": "⚡ キャッシュから送信中...",
        "welcome": "👋 <b>Music Stream HQ へようこそ！</b> 🎧",
        "offline_help": "✈️ このチャットから曲をスマホに保存。",
        "btn_go_premium": "⭐ プレミアムへ（スター）",
        "btn_offline": "✈️ オフライン再生の方法",
        "btn_lang": "🌍 言語を変更",
        "admin_header": "📊 <b>管理者パネル</b>",
        "admin_denied": "⛔ アクセス拒否。",
        "url_processing": "🔗 リンクを処理中...",
        "closed": "✅ 検索を閉じました。",
        "cmd_start": "ボットを起動",
        "cmd_language": "言語を変更",
        "cmd_help": "ヘルプ",
    },
}

LANG_FLAGS = {
    "it": "🇮🇹 Italiano", "en": "🇬🇧 English", "es": "🇪🇸 Español",
    "fr": "🇫🇷 Français", "de": "🇩🇪 Deutsch", "pt": "🇵🇹 Português",
    "ru": "🇷🇺 Русский", "uz": "🇺🇿 O'zbek", "hi": "🇮🇳 हिन्दी",
    "zh": "🇨🇳 中文", "ja": "🇯🇵 日本語",
}

SHAZAM_TEXTS = {
    "it": {"shz_analyzing": "🎧 Analisi audio in corso… attendi qualche secondo ⏳",
           "shz_found": "🎶 <b>Brano Riconosciuto!</b>\n\n📌 <b>Titolo:</b> {title}\n👤 <b>Artista:</b> {artist}",
           "shz_not_found": "❌ Nessun brano riconosciuto in questo audio. Riprova con un audio più chiaro o di durata maggiore!",
           "shz_download": "⬇️ Scarica Brano (MP3)"},
    "en": {"shz_analyzing": "🎧 Analyzing audio… hold on a few seconds ⏳",
           "shz_found": "🎶 <b>Track Recognized!</b>\n\n📌 <b>Title:</b> {title}\n👤 <b>Artist:</b> {artist}",
           "shz_not_found": "❌ No track recognized in this audio. Try again with clearer or longer audio!",
           "shz_download": "⬇️ Download Track (MP3)"},
    "es": {"shz_analyzing": "🎧 Analizando el audio… espera unos segundos ⏳",
           "shz_found": "🎶 <b>¡Canción Reconocida!</b>\n\n📌 <b>Título:</b> {title}\n👤 <b>Artista:</b> {artist}",
           "shz_not_found": "❌ No se reconoció ninguna canción en este audio.",
           "shz_download": "⬇️ Descargar Canción (MP3)"},
    "fr": {"shz_analyzing": "🎧 Analyse de l'audio en cours… ⏳",
           "shz_found": "🎶 <b>Morceau Reconnu !</b>\n\n📌 <b>Titre :</b> {title}\n👤 <b>Artiste :</b> {artist}",
           "shz_not_found": "❌ Aucun morceau reconnu.",
           "shz_download": "⬇️ Télécharger le Morceau (MP3)"},
    "de": {"shz_analyzing": "🎧 Audio wird analysiert… ⏳",
           "shz_found": "🎶 <b>Song erkannt!</b>\n\n📌 <b>Titel:</b> {title}\n👤 <b>Künstler:</b> {artist}",
           "shz_not_found": "❌ Kein Song erkannt.",
           "shz_download": "⬇️ Song herunterladen (MP3)"},
    "pt": {"shz_analyzing": "🎧 Analisando o áudio… ⏳",
           "shz_found": "🎶 <b>Música Reconhecida!</b>\n\n📌 <b>Título:</b> {title}\n👤 <b>Artista:</b> {artist}",
           "shz_not_found": "❌ Nenhuma música reconhecida.",
           "shz_download": "⬇️ Baixar Música (MP3)"},
    "ru": {"shz_analyzing": "🎧 Анализ аудио… ⏳",
           "shz_found": "🎶 <b>Трек распознан!</b>\n\n📌 <b>Название:</b> {title}\n👤 <b>Исполнитель:</b> {artist}",
           "shz_not_found": "❌ Трек не распознан.",
           "shz_download": "⬇️ Скачать трек (MP3)"},
    "uz": {"shz_analyzing": "🎧 Audio tahlil qilinmoqda… ⏳",
           "shz_found": "🎶 <b>Qo'shiq aniqlandi!</b>\n\n📌 <b>Nomi:</b> {title}\n👤 <b>Ijrochi:</b> {artist}",
           "shz_not_found": "❌ Qo'shiq aniqlanmadi.",
           "shz_download": "⬇️ Qo'shiqni yuklab olish (MP3)"},
    "hi": {"shz_analyzing": "🎧 ऑडियो का विश्लेषण हो रहा है… ⏳",
           "shz_found": "🎶 <b>गाना पहचाना गया!</b>\n\n📌 <b>शीर्षक:</b> {title}\n👤 <b>कलाकार:</b> {artist}",
           "shz_not_found": "❌ कोई गाना नहीं पहचाना गया।",
           "shz_download": "⬇️ गाना डाउनलोड करें (MP3)"},
    "zh": {"shz_analyzing": "🎧 正在分析音频… ⏳",
           "shz_found": "🎶 <b>歌曲已识别！</b>\n\n📌 <b>标题：</b>{title}\n👤 <b>歌手：</b>{artist}",
           "shz_not_found": "❌ 未能识别歌曲。",
           "shz_download": "⬇️ 下载歌曲 (MP3)"},
    "ja": {"shz_analyzing": "🎧 オーディオを解析中… ⏳",
           "shz_found": "🎶 <b>曲を認識しました！</b>\n\n📌 <b>タイトル:</b> {title}\n👤 <b>アーティスト:</b> {artist}",
           "shz_not_found": "❌ 曲を認識できませんでした。",
           "shz_download": "⬇️ 曲をダウンロード (MP3)"},
}
for _code, _d in SHAZAM_TEXTS.items():
    LANGUAGES[_code].update(_d)

URL_PATTERNS = re.compile(
    r"(https?://)?(www\.)?"
    r"(youtube\.com/watch|youtube\.com/shorts/|youtu\.be/|soundcloud\.com/|tiktok\.com/|instagram\.com/(reel|p)/|open\.spotify\.com/)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Database (WAL mode for speed)
# ---------------------------------------------------------------------------

class _PooledConnection(sqlite3.Connection):
    def close(self):
        pass
    def real_close(self):
        super().close()

_db_local = threading.local()


def get_db() -> sqlite3.Connection:
    conn = getattr(_db_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(
            DB_PATH, timeout=10, factory=_PooledConnection, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        _db_local.conn = conn
    else:
        try:
            if conn.in_transaction:
                conn.rollback()
        except sqlite3.Error:
            try:
                conn.real_close()
            except Exception:
                pass
            _db_local.conn = None
            return get_db()
    return conn


def init_db() -> None:
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id   INTEGER PRIMARY KEY,
            username  TEXT,
            language_code TEXT NOT NULL DEFAULT 'en',
            last_active   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS downloads (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id   TEXT    UNIQUE NOT NULL,
            file_id    TEXT    NOT NULL,
            title      TEXT,
            performer  TEXT,
            duration   INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_downloads_title ON downloads(title);
        CREATE INDEX IF NOT EXISTS idx_downloads_created ON downloads(created_at);
        CREATE TABLE IF NOT EXISTS stats (
            key   TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        );
    """)
    for ddl in (
        "ALTER TABLE users ADD COLUMN is_premium INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN premium_until TEXT",
        "ALTER TABLE users ADD COLUMN daily_downloads_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN last_download_date TEXT",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
    log.info("Database initialised at %s", DB_PATH)


def upsert_user(user_id: int, username: str | None) -> None:
    conn = get_db()
    conn.execute(
        """INSERT INTO users (user_id, username, last_active)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(user_id) DO UPDATE SET
               username = excluded.username, last_active = CURRENT_TIMESTAMP""",
        (user_id, username),
    )
    conn.commit()
    conn.close()


def get_user_lang(user_id: int) -> str:
    cached = lang_cache.get(user_id)
    if cached is not None:
        return cached
    conn = get_db()
    row = conn.execute(
        "SELECT language_code FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    lang = row["language_code"] if row else "en"
    lang_cache[user_id] = lang
    return lang


def set_user_lang(user_id: int, lang: str) -> None:
    conn = get_db()
    conn.execute("UPDATE users SET language_code = ? WHERE user_id = ?", (lang, user_id))
    conn.commit()
    conn.close()
    lang_cache[user_id] = lang


def get_cached_file_id(video_id: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT file_id, title, performer, duration FROM downloads WHERE video_id = ?",
        (video_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def search_cached_tracks(query: str, limit: int = 10) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT video_id, file_id, title, performer, duration FROM downloads
           WHERE title LIKE ? OR performer LIKE ?
           ORDER BY created_at DESC LIMIT ?""",
        (f"%{query}%", f"%{query}%", limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_cached_tracks(limit: int = 10) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT video_id, file_id, title, performer, duration FROM downloads
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_download(video_id: str, file_id: str, title: str, performer: str, duration: int) -> None:
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO downloads (video_id, file_id, title, performer, duration) VALUES (?, ?, ?, ?, ?)",
        (video_id, file_id, title, performer, duration),
    )
    conn.commit()
    conn.close()


FREE_DAILY_LIMIT = 8
_premium_lkg: dict[int, bool] = {}


def is_premium(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT is_premium, premium_until FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.close()
        if not row or not row["is_premium"]:
            _premium_lkg[user_id] = False
            return False
        until = row["premium_until"]
        if until and datetime.utcnow() > datetime.fromisoformat(until):
            conn = get_db()
            conn.execute("UPDATE users SET is_premium = 0 WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()
            _premium_lkg[user_id] = False
            return False
        _premium_lkg[user_id] = True
        return True
    except Exception as exc:
        log.error("is_premium error: %s", exc)
        return _premium_lkg.get(user_id, False)


def premium_remaining(user_id: int) -> timedelta | None:
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT premium_until FROM users WHERE user_id = ? AND is_premium = 1", (user_id,)
        ).fetchone()
        conn.close()
        if row and row["premium_until"]:
            delta = datetime.fromisoformat(row["premium_until"]) - datetime.utcnow()
            return delta if delta.total_seconds() > 0 else None
    except Exception:
        pass
    return None


def grant_premium(user_id: int, days: int) -> datetime:
    conn = get_db()
    row = conn.execute(
        "SELECT premium_until FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    base = datetime.utcnow()
    if row and row["premium_until"]:
        try:
            cur = datetime.fromisoformat(row["premium_until"])
            if cur > base:
                base = cur
        except ValueError:
            pass
    until = base + timedelta(days=days)
    conn.execute(
        """INSERT INTO users (user_id, is_premium, premium_until, daily_downloads_count)
           VALUES (?, 1, ?, 0)
           ON CONFLICT(user_id) DO UPDATE SET
               is_premium = 1,
               premium_until = excluded.premium_until,
               daily_downloads_count = 0""",
        (user_id, until.isoformat()),
    )
    conn.commit()
    conn.close()
    _premium_lkg[user_id] = True
    return until


def check_daily_limit(user_id: int) -> bool:
    if is_premium(user_id):
        return True
    today = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        conn.execute(
            """UPDATE users SET
                   daily_downloads_count = CASE
                       WHEN last_download_date = ? THEN daily_downloads_count + 1
                       ELSE 1 END,
                   last_download_date = ?
               WHERE user_id = ?""",
            (today, today, user_id),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error("check_daily_limit error: %s", exc)
    return True


def get_admin_stats() -> dict:
    conn = get_db()
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    monthly_active = conn.execute(
        "SELECT COUNT(*) FROM users WHERE last_active >= ?",
        (datetime.utcnow() - timedelta(days=30),),
    ).fetchone()[0]
    row = conn.execute("SELECT value FROM stats WHERE key = 'songs_delivered'").fetchone()
    delivered = row[0] if row else 0
    cache_count = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
    total_downloads = max(delivered, cache_count)
    premium_users = conn.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1").fetchone()[0]
    conn.close()
    return {"total_users": total_users, "monthly_active": monthly_active,
            "total_downloads": total_downloads, "premium_users": premium_users}


def bump_stat(key: str, by: int = 1) -> None:
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO stats (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = stats.value + excluded.value""",
            (key, by),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error("bump_stat %s failed: %s", key, exc)


def t(user_id: int, key: str) -> str:
    strings = LANGUAGES.get(get_user_lang(user_id), LANGUAGES["en"])
    return strings.get(key, LANGUAGES["en"].get(key, key))

# ---------------------------------------------------------------------------
# Music search and yt-dlp helpers
# ---------------------------------------------------------------------------

MIN_TRACK_SECONDS = 1
MAX_TRACK_SECONDS = 600

EQ_FILTER = (
    "highpass=f=25,"
    "bass=g=6:f=90:w=0.6,"
    "equalizer=f=250:width_type=q:width=1.2:g=-1.5,"
    "equalizer=f=3200:width_type=q:width=1.0:g=3,"
    "treble=g=3.5:f=7800,"
    "equalizer=f=12500:width_type=q:width=0.8:g=3,"
    "acompressor=threshold=-16dB:ratio=2:attack=12:release=180:makeup=2,"
    "dynaudnorm=f=300:g=17:p=0.9,alimiter=limit=0.891:level=false"
)

_search_result_cache: dict[str, dict] = {}
_search_cache_lock = threading.Lock()
SEARCH_CACHE_TTL = 300
SEARCH_CACHE_MAX = 500


def _ydl_opts_base() -> dict:
    """Base yt-dlp options for YouTube search and audio extraction."""
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
        **YDL_EXTRACTOR_ARGS,
        **YDL_JS_RUNTIMES,
    }
    if _YT_COOKIES_FILE:
        opts["cookiefile"] = _YT_COOKIES_FILE
    return opts


def _valid_track_duration(duration: int) -> bool:
    return MIN_TRACK_SECONDS <= duration <= MAX_TRACK_SECONDS


def _search_ytmusic_songs(query: str, max_results: int) -> list[dict]:
    """Search YouTube Music's song catalogue, never generic YouTube videos."""
    session = requests.Session()
    session.request = functools.partial(session.request, timeout=10)
    rows = YTMusic(requests_session=session).search(
        query, filter="songs", limit=max(20, max_results)
    )
    results: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        video_id = row.get("videoId") or ""
        if len(video_id) != 11 or video_id in seen:
            continue
        try:
            duration = int(row.get("duration_seconds") or 0)
        except (TypeError, ValueError):
            continue
        if not _valid_track_duration(duration):
            continue
        artists = ", ".join(
            artist.get("name", "").strip()
            for artist in (row.get("artists") or [])
            if artist.get("name")
        ) or "Unknown"
        thumbnails = row.get("thumbnails") or []
        thumbnail = thumbnails[-1].get("url", "") if thumbnails else (
            f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
        )
        results.append({
            "id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": row.get("title") or "Unknown",
            "uploader": artists,
            "duration": duration,
            "thumbnail": thumbnail,
            "source": "youtube_music",
        })
        seen.add(video_id)
        if len(results) >= max_results:
            break
    return results


def _search_soundcloud_tracks(query: str, max_results: int) -> list[dict]:
    opts = {
        **_ydl_opts_base(),
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": True,
    }
    results: list[dict] = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"scsearch{max_results}:{query}", download=False)
    for entry in (info or {}).get("entries", []):
        if not entry or not entry.get("url"):
            continue
        try:
            duration = int(entry.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        if not _valid_track_duration(duration):
            continue
        thumbnails = entry.get("thumbnails") or []
        results.append({
            "id": f"sc:{entry['id']}" if entry.get("id") else "",
            "url": entry["url"],
            "title": entry.get("title") or "Unknown",
            "uploader": entry.get("uploader") or "Unknown",
            "duration": duration,
            "thumbnail": thumbnails[-1].get("url", "") if thumbnails else "",
            "source": "soundcloud",
        })
    return results


def search_youtube(query: str, max_results: int = SEARCH_POOL) -> list[dict]:
    """Return actual songs from music catalogues, not generic YouTube videos."""
    cache_key = f"{query.strip().lower()}|{max_results}"
    now = time.time()
    with _search_cache_lock:
        hit = _search_result_cache.get(cache_key)
        if hit and now - hit["ts"] < SEARCH_CACHE_TTL:
            return hit["results"]

    results: list[dict] = []
    try:
        results = _search_ytmusic_songs(query, max_results)
        log.info("YouTube Music returned %d songs for %r", len(results), query)
    except Exception as exc:
        log.warning("YouTube Music search failed: %s", exc)

    if not results:
        try:
            results = _search_soundcloud_tracks(query, min(max_results, 15))
            log.info("SoundCloud returned %d tracks for %r", len(results), query)
        except Exception as exc:
            log.warning("SoundCloud fallback failed: %s", exc)

    if results:
        with _search_cache_lock:
            if len(_search_result_cache) >= SEARCH_CACHE_MAX:
                oldest = sorted(_search_result_cache.items(), key=lambda kv: kv[1]["ts"])
                for k, _ in oldest[: SEARCH_CACHE_MAX // 2]:
                    _search_result_cache.pop(k, None)
            _search_result_cache[cache_key] = {"ts": time.time(), "results": results}
    return results


def get_audio_stream_url(video_url: str) -> dict | None:
    opts = {
        **_ydl_opts_base(),
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            return {
                "audio_url": info.get("url") or "",
                "title": info.get("title", "Unknown"),
                "uploader": info.get("uploader") or info.get("channel") or "Unknown",
                "duration": info.get("duration") or 0,
                "video_id": info.get("id", ""),
            }
    except Exception as exc:
        log.warning("Stream extraction failed: %s", exc)
        return None


def download_audio(video_url: str) -> tuple[str | None, dict]:
    tmpdir = tempfile.mkdtemp(prefix="musicbot_")
    opts = {
        **_ydl_opts_base(),
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail"},
        ],
        "postprocessor_args": {"ffmpegextractaudio": ["-af", EQ_FILTER, "-compression_level", "9"]},
        "writethumbnail": True,
        "concurrent_fragment_downloads": 4,
        "noplaylist": True,
        "match_filter": yt_dlp.utils.match_filter_func(f"duration<={MAX_TRACK_SECONDS}"),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
        if info and info.get("entries") is not None:
            info = info["entries"][0] if info["entries"] else None
        if info and int(info.get("duration") or 0) > MAX_TRACK_SECONDS:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None, {"error": "too_long"}
        for f in Path(tmpdir).iterdir():
            if f.suffix.lower() == ".mp3":
                return str(f), {
                    "video_id": info.get("id", ""),
                    "title": info.get("title", "Unknown"),
                    "performer": info.get("uploader") or info.get("channel") or "Unknown",
                    "duration": int(info.get("duration") or 0),
                    "thumbnail": info.get("thumbnail") or "",
                }
    except Exception as exc:
        log.error("Download error for %s: %s", video_url, exc)
        shutil.rmtree(tmpdir, ignore_errors=True)
    return None, {}


def clean_title(title: str, uploader: str = "") -> str:
    title = _INVISIBLE_RE.sub("", title or "")
    title = title.split("|")[0].strip().rstrip("-–—").strip()
    uploader = re.sub(r"\s*-\s*Topic$", "", uploader or "").strip()
    if any(sep in title for sep in (" - ", " – ", " — ")) or not uploader:
        return title
    return f"{uploader} – {title}"


def format_duration(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Results keyboard & message
# ---------------------------------------------------------------------------

def build_results_keyboard() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.row(*[types.KeyboardButton(str(i)) for i in range(1, 6)])
    markup.row(*[types.KeyboardButton(str(i)) for i in range(6, 11)])
    markup.row(types.KeyboardButton(BTN_PREV), types.KeyboardButton(BTN_CLOSE), types.KeyboardButton(BTN_NEXT))
    return markup


def build_results_text(query: str, results: list[dict], offset: int) -> str:
    total_pages = max(1, -(-len(results) // PAGE_SIZE))
    cur_page = offset // PAGE_SIZE + 1
    lines = [f"🎙 <b>{esc(query)}</b>  📄 {cur_page}/{total_pages}", ""]
    page = results[offset:offset + PAGE_SIZE]
    for i, r in enumerate(page, start=1):
        dur = format_duration(r["duration"])
        lines.append(f"<b>{i}.</b> {esc(clean_title(r['title'], r['uploader']))} <i>({dur})</i>")
    return "\n".join(lines)


def send_results_page(chat_id: int, uid: int) -> None:
    state = user_search_cache.get(uid)
    if not state:
        return
    text = build_results_text(state["query"], state["results"], state["offset"])
    bot.send_message(chat_id, text, reply_markup=build_results_keyboard())


# ---------------------------------------------------------------------------
# Lyrics (lrclib.net — free, no API key)
# ---------------------------------------------------------------------------

BOT_USERNAME = "MusicStreeambot"


def lyrics_keyboard(video_id: str) -> types.InlineKeyboardMarkup | None:
    if not video_id:
        return None
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📝 Lyrics", callback_data=f"lyr:{video_id}"))
    return markup


def lyrics_keyboard_inline(video_id: str) -> types.InlineKeyboardMarkup | None:
    if not video_id:
        return None
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        "📝 Lyrics", url=f"https://t.me/{BOT_USERNAME}?start=lyr_{video_id}"
    ))
    return markup


_NOISE_RE = re.compile(
    r"\s*[\(\[][^)\]]*(official|video|audio|lyric|lyrics|hd|hq|4k|remaster|"
    r"visualizer|explicit|clip|prod\.?|mv|m/v|live|cover art|testo|paroles)[^)\]]*[\)\]]",
    re.IGNORECASE,
)


def normalize_track(title: str, performer: str) -> tuple[str, str]:
    title = _INVISIBLE_RE.sub("", title or "")
    performer = re.sub(r"\s*-\s*Topic$", "", _INVISIBLE_RE.sub("", performer or "")).strip()
    title = _NOISE_RE.sub("", title)
    title = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*$", "", title).strip()
    for sep in (" - ", " – ", " — "):
        if sep in title:
            left, right = title.split(sep, 1)
            if left.strip() and right.strip():
                return right.strip(), left.strip()
    title = re.sub(r"\s+(ft\.?|feat\.?)\s+.*$", "", title, flags=re.IGNORECASE).strip()
    return title, performer


def _lyrics_lrclib(track: str, artist: str) -> str | None:
    for params in (
        {"track_name": track, "artist_name": artist},
        {"q": f"{artist} {track}".strip()},
        {"q": track},
    ):
        try:
            resp = requests.get(
                "https://lrclib.net/api/search", params=params, timeout=6,
                headers={"User-Agent": "MusicStreamBot"},
            )
            if resp.ok:
                for item in resp.json():
                    text = item.get("plainLyrics")
                    if text and text.strip():
                        return text.strip()
        except Exception:
            pass
    return None


def _lyrics_ovh(track: str, artist: str) -> str | None:
    if not artist:
        return None
    try:
        resp = requests.get(
            f"https://api.lyrics.ovh/v1/{requests.utils.quote(artist)}/{requests.utils.quote(track)}",
            timeout=6,
        )
        if resp.ok:
            text = (resp.json().get("lyrics") or "").strip()
            if text:
                return text
    except Exception:
        pass
    return None


def fetch_lyrics(title: str, performer: str) -> str | None:
    track, artist = normalize_track(title, performer)
    for source in (_lyrics_lrclib, _lyrics_ovh):
        text = source(track, artist)
        if text:
            return text
    if artist:
        text = _lyrics_lrclib(artist, track)
        if text:
            return text
    return None


@bot.callback_query_handler(func=lambda call: call.data.startswith("lyr:"))
def cb_lyrics(call: types.CallbackQuery):
    uid = call.from_user.id
    video_id = call.data.split(":", 1)[1]
    bot.answer_callback_query(call.id, "🔎 ...")

    def worker():
        track = get_cached_file_id(video_id)
        if not track:
            bot.send_message(call.message.chat.id, t(uid, "lyrics_not_found"))
            return
        text = fetch_lyrics(track["title"], track["performer"])
        if not text:
            bot.send_message(call.message.chat.id, t(uid, "lyrics_not_found"))
            return
        header = f"📝 <b>{esc(clean_title(track['title'], track['performer']))}</b>\n\n"
        body = esc(text[:3900] + ("…" if len(text) > 3900 else ""))
        bot.send_message(call.message.chat.id, header + body)

    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Premium UI & Telegram Stars payments
# ---------------------------------------------------------------------------

PREMIUM_TEXTS = {
    "it": {
        "limit_reached": "⚠️ <b>Download momentaneamente finiti per oggi!</b>\n\nHai raggiunto il tuo limite giornaliero di <b>8 canzoni gratuite</b>.\n\nAbbonati subito a Premium con le Stelle di Telegram! 🚀",
        "playlist_premium": "📂 <b>Download Playlist Riservato ai Membri Premium!</b>\n\nPassa a Premium con le Stelle di Telegram!",
        "locked_alert": "🔒 Funzione Riservata ai Membri Premium!",
        "subscribe_text": "⭐ <b>Music Stream HQ Premium</b>\n\n✅ Download playlist e album interi\n✅ 🎤 Vocal Remover (karaoke)\n✅ 🌀 Audio 8D\n✅ 🎛️ Audio FX Studio\n\nScegli il tuo piano:",
        "status_free": "👤 <b>Account: FREE</b>\n\n⬇️ Download oggi: {used} — illimitati e gratuiti ✅\n\nPassa a Premium con /subscribe! ⭐",
        "status_premium": "💎 <b>Account: PREMIUM</b>\n\n⏳ Tempo rimanente: <b>{days}g {hours}h {minutes}m</b>\n\nGrazie del supporto! 🚀",
        "payment_ok": "🎉 <b>Pagamento ricevuto — Benvenuto in Premium!</b>\n\n💎 Valido fino al: <b>{until}</b> (UTC)\n\nTutte le funzioni VIP sono attive da ORA! 🚀",
        "granted": "🎁 <b>Hai ricevuto {days} giorni di Premium!</b>\n\n💎 Valido fino al: <b>{until}</b> (UTC). Goditi le funzioni VIP! 🚀",
        "processing_fx": "🎛 Elaborazione audio in corso... ⏳",
        "fx_menu": "🎛️ <b>Audio FX Studio</b>\n\n👇 <b>Scegli un effetto:</b>",
        "fx_error": "❌ Elaborazione non riuscita. Riprova più tardi.",
        "vip_alert": "💎 Sei un membro Premium! Usa /status per i dettagli.",
        "btn_unlock": "⭐ Sblocca Premium",
        "btn_vip": "💎 Status VIP",
    },
    "en": {
        "limit_reached": "⚠️ <b>No more downloads for today!</b>\n\nYou reached your daily limit of <b>8 free songs</b>.\n\nSubscribe to Premium with Telegram Stars! 🚀",
        "playlist_premium": "📂 <b>Playlist downloads are for Premium members!</b>\n\nGo Premium with Telegram Stars!",
        "locked_alert": "🔒 Premium members only!",
        "subscribe_text": "⭐ <b>Music Stream HQ Premium</b>\n\n✅ Full playlist & album downloads\n✅ 🎤 Vocal Remover\n✅ 🌀 8D Audio\n✅ 🎛️ Audio FX Studio\n\nPick your plan:",
        "status_free": "👤 <b>Account: FREE</b>\n\n⬇️ Downloads today: {used} — unlimited and free ✅\n\nGo Premium with /subscribe! ⭐",
        "status_premium": "💎 <b>Account: PREMIUM</b>\n\n⏳ Time left: <b>{days}d {hours}h {minutes}m</b>\n\nThanks for your support! 🚀",
        "payment_ok": "🎉 <b>Payment received — Welcome to Premium!</b>\n\n💎 Valid until: <b>{until}</b> (UTC)\n\nAll VIP features are active NOW! 🚀",
        "granted": "🎁 <b>You received {days} days of Premium!</b>\n\n💎 Valid until: <b>{until}</b> (UTC). Enjoy the VIP features! 🚀",
        "processing_fx": "🎛 Processing audio... ⏳",
        "fx_menu": "🎛️ <b>Audio FX Studio</b>\n\n👇 <b>Pick an effect:</b>",
        "fx_error": "❌ Processing failed. Try again later.",
        "vip_alert": "💎 You are a Premium member! Use /status for details.",
        "btn_unlock": "⭐ Unlock Premium",
        "btn_vip": "💎 VIP Status",
    },
}


def pxt(user_id: int, key: str) -> str:
    lang = get_user_lang(user_id)
    base = PREMIUM_TEXTS.get(lang, PREMIUM_TEXTS["en"])
    return base.get(key, PREMIUM_TEXTS["en"][key])


PLANS = [
    ("🚀 1 Day (Trial) — 20 ⭐️", 1, 20),
    ("⭐ 1 Month — 100 ⭐️", 30, 100),
    ("💫 3 Months — 250 ⭐️", 90, 250),
    ("👑 6 Months — 500 ⭐️", 180, 500),
]

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_PLAYLIST_RE = re.compile(r"[?&]list=|/playlist|/album/|/sets/[^/]+/?$|/sets/", re.IGNORECASE)

EFFECTS = {
    "vocal": ("🎤 Vocal Remover (Karaoke)",
              "asplit[a][b];[a]pan=mono|c0=0.5*c0+-0.5*c1,highpass=f=110[side];"
              "[b]pan=mono|c0=0.5*c0+0.5*c1,lowpass=f=150[low];"
              "[side][low]amix=inputs=2:normalize=0,"
              "pan=stereo|c0=c0|c1=c0,"
              "equalizer=f=2800:width_type=q:width=1.0:g=2,"
              "treble=g=2.5:f=6500,"
              "aecho=0.55:0.4:14:0.08,"
              "dynaudnorm=f=250:g=15,"
              "dynaudnorm=f=300:g=17:p=0.9,alimiter=limit=0.871:level=false[out]"),
    "8d": ("🌀 Audio 8D", "apulsator=hz=0.08,stereowiden=delay=18:feedback=0.4:crossfeed=0.35,bass=g=3:f=100:w=0.5,treble=g=1.5:f=8000,dynaudnorm=f=300:g=17:p=0.9,alimiter=limit=0.871:level=false"),
    "bass": ("🔊 Bass Boost", "bass=g=11:f=85:w=0.5,equalizer=f=45:width_type=q:width=1.2:g=3,acompressor=threshold=-12dB:ratio=3:attack=10:release=160:makeup=2.5,alimiter=limit=0.94,dynaudnorm=f=300:g=17:p=0.9,alimiter=limit=0.891:level=false"),
    "bassx": ("💣 Bass EXTREME", "highpass=f=28,bass=g=16:f=65:w=0.55,equalizer=f=120:width_type=q:width=1.0:g=4,acompressor=threshold=-11dB:ratio=4:attack=8:release=140:makeup=3,alimiter=limit=0.89:attack=4:release=60,dynaudnorm=f=300:g=17:p=0.9,alimiter=limit=0.933:level=false"),
    "night": ("⚡ Nightcore", "asetrate=44100*1.25,aresample=44100,bass=g=4:f=110:w=0.5,treble=g=1.5:f=9000,dynaudnorm=f=300:g=17:p=0.9,alimiter=limit=0.891:level=false"),
    "speed": ("🚀 Speed Up", "atempo=1.25,treble=g=1:f=8000,dynaudnorm=f=300:g=17:p=0.9,alimiter=limit=0.891:level=false"),
    "slow": ("🌙 Slowed + Reverb", "asetrate=44100*0.85,aresample=44100,bass=g=2.5:f=100:w=0.5,aecho=0.78:0.82:50|70|110:0.22|0.18|0.12,lowpass=f=15000,dynaudnorm=f=300:g=17:p=0.9,alimiter=limit=0.871:level=false"),
    "lofi": ("🎧 Lo-Fi Chill", "highpass=f=60,lowpass=f=4200,asetrate=44100*0.97,aresample=44100,bass=g=2:f=120:w=0.5,aecho=0.7:0.7:40:0.16,acompressor=threshold=-16dB:ratio=2.5:attack=20:release=250:makeup=2,dynaudnorm=f=300:g=17:p=0.9,alimiter=limit=0.841:level=false"),
    "hall": ("🏟️ Live Concert", "highpass=f=55,aecho=0.72:0.55:220|360|520:0.28|0.2|0.12,lowpass=f=8500,dynaudnorm=f=300:g=17:p=0.9,alimiter=limit=0.841:level=false"),
}
FX_MENU_KEYS = ("bass", "bassx", "night", "speed", "slow", "lofi", "hall")


def track_keyboard(video_id: str, user_id: int) -> types.InlineKeyboardMarkup | None:
    if not video_id:
        return None
    premium = is_premium(user_id)
    lock = "" if premium else " 🔒"
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(f"🎤 Vocal Remover{lock}", callback_data=f"eff:vocal:{video_id}"),
        types.InlineKeyboardButton(f"🌀 Audio 8D{lock}", callback_data=f"eff:8d:{video_id}"),
    )
    kb.add(
        types.InlineKeyboardButton("🎛️ Audio FX", callback_data=f"fxm:{video_id}"),
        types.InlineKeyboardButton("📜 Testo / Lyrics", callback_data=f"lyr:{video_id}"),
    )
    if premium:
        kb.add(types.InlineKeyboardButton(pxt(user_id, "btn_vip"), callback_data="vip"))
    else:
        kb.add(types.InlineKeyboardButton(pxt(user_id, "btn_unlock"), callback_data="sub"))
    return kb


def subscribe_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for label, days, stars in PLANS:
        kb.add(types.InlineKeyboardButton(label, callback_data=f"buy:{days}:{stars}"))
    return kb


def send_subscribe_panel(chat_id: int, user_id: int) -> None:
    try:
        bot.send_message(chat_id, pxt(user_id, "subscribe_text"), reply_markup=subscribe_keyboard())
    except Exception as exc:
        log.error("subscribe panel error: %s", exc)


@bot.message_handler(commands=["subscribe"])
def cmd_subscribe(message: types.Message):
    if not command_ok(message):
        return
    upsert_user(message.from_user.id, message.from_user.username)
    send_subscribe_panel(message.chat.id, message.from_user.id)


@bot.callback_query_handler(func=lambda c: c.data == "sub")
def cb_sub(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    if call.message:
        send_subscribe_panel(call.message.chat.id, call.from_user.id)


@bot.callback_query_handler(func=lambda c: c.data == "vip")
def cb_vip(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id, pxt(call.from_user.id, "vip_alert"), show_alert=True)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("buy:"))
def cb_buy(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
        _, days, stars = call.data.split(":")
        days, stars = int(days), int(stars)
        label = next((l for l, d, s in PLANS if d == days), f"{days} days")
        bot.send_invoice(
            call.message.chat.id,
            title="Music Stream HQ Premium",
            description=f"Premium access — {label.split('—')[0].strip()}",
            invoice_payload=f"premium_{days}",
            provider_token="",
            currency="XTR",
            prices=[types.LabeledPrice(label=f"Premium {days}d", amount=stars)],
        )
    except Exception as exc:
        log.error("send_invoice error: %s", exc)


def _validate_plan(payload: str, currency: str, amount: int) -> int | None:
    if currency != "XTR" or not payload.startswith("premium_"):
        return None
    try:
        days = int(payload.split("_", 1)[1])
    except (ValueError, IndexError):
        return None
    for _label, plan_days, stars in PLANS:
        if plan_days == days and stars == amount:
            return days
    return None


@bot.pre_checkout_query_handler(func=lambda q: True)
def on_pre_checkout(query: types.PreCheckoutQuery):
    try:
        if _validate_plan(query.invoice_payload or "", query.currency, query.total_amount) is not None:
            bot.answer_pre_checkout_query(query.id, ok=True)
        else:
            bot.answer_pre_checkout_query(query.id, ok=False, error_message="Invalid plan.")
    except Exception as exc:
        log.error("pre_checkout error: %s", exc)


@bot.message_handler(content_types=["successful_payment"])
def on_successful_payment(message: types.Message):
    try:
        uid = message.from_user.id
        sp = message.successful_payment
        days = _validate_plan(sp.invoice_payload or "", sp.currency, sp.total_amount)
        if days is None:
            bot.send_message(message.chat.id, "⚠️ Payment received but plan unrecognized — contact support: /help")
            return
        until = None
        for attempt in range(5):
            try:
                until = grant_premium(uid, days)
                break
            except Exception as exc:
                log.error("grant_premium attempt %d failed for %s: %s", attempt + 1, uid, exc)
                time.sleep(1)
        if until is None:
            bot.send_message(message.chat.id, "⚠️ Payment received! Contact support if Premium isn't active: /help")
            return
        bot.send_message(message.chat.id, pxt(uid, "payment_ok").format(until=until.strftime("%d/%m/%Y %H:%M")))
    except Exception as exc:
        log.error("successful_payment error: %s", exc)


@bot.message_handler(commands=["status"])
def cmd_status(message: types.Message):
    if not command_ok(message):
        return
    uid = message.from_user.id
    upsert_user(uid, message.from_user.username)
    try:
        remaining = premium_remaining(uid) if is_premium(uid) else None
        if remaining:
            total_min = int(remaining.total_seconds() // 60)
            bot.send_message(message.chat.id, pxt(uid, "status_premium").format(
                days=total_min // 1440, hours=(total_min % 1440) // 60, minutes=total_min % 60))
        else:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            conn = get_db()
            row = conn.execute(
                "SELECT daily_downloads_count, last_download_date FROM users WHERE user_id = ?", (uid,)
            ).fetchone()
            conn.close()
            used = row["daily_downloads_count"] if row and row["last_download_date"] == today else 0
            bot.send_message(message.chat.id, pxt(uid, "status_free").format(used=used))
    except Exception as exc:
        log.error("/status error: %s", exc)


def _resolve_user(token: str) -> int | None:
    token = token.strip()
    if token.lstrip("-").isdigit():
        return int(token)
    username = token.lstrip("@")
    if not username:
        return None
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (username,)
        ).fetchone()
        conn.close()
        return row["user_id"] if row else None
    except Exception:
        return None


@bot.message_handler(commands=["grant", "regala", "reagala"])
def cmd_grant(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.split()
        if len(parts) < 3:
            raise ValueError
        target_token = parts[1]
        days_token = next((p for p in parts[2:] if p.isdigit()), None)
        if days_token is None:
            raise ValueError
        days = int(days_token)
        target = _resolve_user(target_token)
        if target is None:
            bot.send_message(message.chat.id, f"❌ Utente {esc(target_token)} non trovato.")
            return
        until = grant_premium(target, days)
        bot.send_message(message.chat.id,
                         f"🎁 Premium regalato a <code>{target}</code> — {days} giorni "
                         f"(fino al {until.strftime('%d/%m/%Y %H:%M')} UTC) ✅")
        try:
            bot.send_message(target, pxt(target, "granted").format(
                days=days, until=until.strftime("%d/%m/%Y %H:%M")))
        except Exception:
            pass
    except ValueError:
        bot.send_message(message.chat.id, "Uso: /regala @username 30  oppure  /regala <user_id> <giorni>")
    except Exception as exc:
        log.error("/regala error: %s", exc)


fx_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="fx")
_active_fx: set[int] = set()
_active_fx_lock = threading.Lock()

FX_CACHE_DIR = os.path.join(tempfile.gettempdir(), "fx_src_cache")
FX_CACHE_TTL = 1800
_fx_cache: dict[str, tuple[float, str, dict]] = {}
_fx_cache_lock = threading.Lock()
_fx_dl_locks: dict[str, threading.Lock] = {}


def _fx_video_lock(video_id: str) -> threading.Lock:
    with _fx_cache_lock:
        lock = _fx_dl_locks.get(video_id)
        if lock is None:
            lock = _fx_dl_locks[video_id] = threading.Lock()
        return lock


def _download_fx_source(video_id: str):
    tmpdir = tempfile.mkdtemp(prefix="fxsrc_")
    opts = {
        **_ydl_opts_base(),
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "src.%(ext)s"),
        "noplaylist": True,
        "match_filter": yt_dlp.utils.match_filter_func(f"duration<={MAX_TRACK_SECONDS}"),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
        files = [f for f in Path(tmpdir).iterdir() if f.is_file()]
        if info and files:
            return str(files[0]), {
                "video_id": info.get("id", ""),
                "title": info.get("title", "Unknown"),
                "performer": info.get("uploader") or info.get("channel") or "Unknown",
                "duration": int(info.get("duration") or 0),
                "raw": True,
            }
    except Exception as exc:
        log.error("FX source download error for %s: %s", video_id, exc)
    shutil.rmtree(tmpdir, ignore_errors=True)
    return None, {}


def _fx_cached_source(video_id: str):
    def _private_copy(path: str, meta: dict):
        fd, copy_path = tempfile.mkstemp(prefix="fx_in_", suffix=os.path.splitext(path)[1] or ".mp3")
        os.close(fd)
        shutil.copyfile(path, copy_path)
        return copy_path, meta

    with _fx_video_lock(video_id):
        now = time.time()
        with _fx_cache_lock:
            hit = _fx_cache.get(video_id)
        if hit and now - hit[0] < FX_CACHE_TTL and os.path.exists(hit[1]):
            return _private_copy(hit[1], hit[2])
        with _fx_cache_lock:
            expired = [(vid, path) for vid, (ts, path, _m) in _fx_cache.items()
                       if now - ts >= FX_CACHE_TTL and vid != video_id]
            for vid, _p in expired:
                _fx_cache.pop(vid, None)
                _fx_dl_locks.pop(vid, None)
        for _vid, path in expired:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)
        mp3_path, meta = _download_fx_source(video_id)
        if not mp3_path:
            return None, None
        os.makedirs(FX_CACHE_DIR, exist_ok=True)
        dest_dir = os.path.join(FX_CACHE_DIR, video_id)
        shutil.rmtree(dest_dir, ignore_errors=True)
        shutil.move(os.path.dirname(mp3_path), dest_dir)
        cached_path = os.path.join(dest_dir, os.path.basename(mp3_path))
        with _fx_cache_lock:
            _fx_cache[video_id] = (now, cached_path, meta)
        return _private_copy(cached_path, meta)


def _fx_cache_seed(video_id: str, mp3_path: str, meta: dict) -> bool:
    if not video_id:
        return False
    try:
        with _fx_video_lock(video_id):
            with _fx_cache_lock:
                hit = _fx_cache.get(video_id)
            if hit and os.path.exists(hit[1]):
                return False
            os.makedirs(FX_CACHE_DIR, exist_ok=True)
            dest_dir = os.path.join(FX_CACHE_DIR, video_id)
            shutil.rmtree(dest_dir, ignore_errors=True)
            shutil.move(os.path.dirname(mp3_path), dest_dir)
            cached_path = os.path.join(dest_dir, os.path.basename(mp3_path))
            with _fx_cache_lock:
                _fx_cache[video_id] = (time.time(), cached_path, meta)
        return True
    except Exception as exc:
        log.warning("fx cache seed failed: %s", exc)
        return False


def _apply_effect_and_send(chat_id: int, user_id: int, video_id: str, effect: str) -> None:
    label, afilter = EFFECTS[effect]
    status = None
    out_path = None
    mp3_path = None
    try:
        status = bot.send_message(chat_id, pxt(user_id, "processing_fx"))
        mp3_path, meta = _fx_cached_source(video_id)
        if not mp3_path:
            bot.send_message(chat_id, pxt(user_id, "fx_error"))
            return
        fd, out_path = tempfile.mkstemp(prefix=f"fx_{effect}_", suffix=".mp3")
        os.close(fd)
        if meta.get("raw"):
            afilter = f"{EQ_FILTER},{afilter}"
        if "[" in afilter:
            filter_args = ["-filter_complex", afilter, "-map", "[out]"]
        else:
            filter_args = ["-af", afilter]
        proc = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", mp3_path, *filter_args, "-b:a", "320k",
             "-compression_level", "7", out_path],
            capture_output=True, timeout=300,
        )
        if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            log.error("ffmpeg effect error: %s", proc.stderr[-300:] if proc.stderr else "?")
            bot.send_message(chat_id, pxt(user_id, "fx_error"))
            return
        with open(out_path, "rb") as f:
            bot.send_audio(
                chat_id, audio=f,
                title=f"{meta['title']} ({label.split(' ', 1)[1]})",
                performer=meta["performer"], duration=meta.get("duration"),
            )
    except Exception as exc:
        log.error("Effect %s failed: %s", effect, exc)
        try:
            bot.send_message(chat_id, pxt(user_id, "fx_error"))
        except Exception:
            pass
    finally:
        with _active_fx_lock:
            _active_fx.discard(user_id)
        for tmp in (out_path, mp3_path):
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        if status:
            try:
                bot.delete_message(chat_id, status.message_id)
            except Exception:
                pass


@bot.callback_query_handler(func=lambda c: c.data.startswith(("eff:", "fxm:", "fxa:")))
def cb_effects(call: types.CallbackQuery):
    uid = call.from_user.id
    chat_id = call.message.chat.id if call.message else None
    if chat_id is None:
        try:
            bot.answer_callback_query(call.id, pxt(uid, "locked_alert"), show_alert=True)
        except Exception:
            pass
        return
    premium = is_premium(uid)
    if not premium and not call.data.startswith("fxm:"):
        try:
            bot.answer_callback_query(call.id, pxt(uid, "locked_alert"), show_alert=True)
        except Exception:
            pass
        send_subscribe_panel(chat_id, uid)
        return
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    try:
        if call.data.startswith("fxm:"):
            video_id = call.data.split(":", 1)[1]
            if not _VIDEO_ID_RE.match(video_id):
                return
            kb = types.InlineKeyboardMarkup(row_width=2)
            buttons = [
                types.InlineKeyboardButton(EFFECTS[key][0], callback_data=f"fxa:{key}:{video_id}")
                for key in FX_MENU_KEYS
            ]
            for i in range(0, len(buttons), 2):
                kb.add(*buttons[i:i + 2])
            if not premium:
                kb.add(types.InlineKeyboardButton(pxt(uid, "btn_unlock"), callback_data="sub"))
            bot.send_message(chat_id, pxt(uid, "fx_menu"), reply_markup=kb)
            return
        parts = call.data.split(":", 2)
        if len(parts) != 3:
            return
        _, effect, video_id = parts
        if effect not in EFFECTS or not _VIDEO_ID_RE.match(video_id):
            return
        with _active_fx_lock:
            if uid in _active_fx:
                return
            _active_fx.add(uid)
        try:
            fx_pool.submit(_apply_effect_and_send, chat_id, uid, video_id, effect)
        except Exception:
            with _active_fx_lock:
                _active_fx.discard(uid)
            raise
    except Exception as exc:
        log.error("cb_effects error: %s", exc)


# ---------------------------------------------------------------------------
# Shazam — audio recognition
# ---------------------------------------------------------------------------

_SHORTVID_RE = re.compile(r"tiktok\.com/|instagram\.com/(?:reel|p)/|youtube\.com/shorts/", re.IGNORECASE)
_SHZ_MAX_FILE = 20 * 1024 * 1024


def _shazam_recognize(path: str) -> dict | None:
    import asyncio
    from shazamio import Shazam
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(Shazam().recognize(path))
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _media_duration(src: str) -> float:
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", src],
            capture_output=True, timeout=30,
        )
        return float(p.stdout.decode().strip())
    except Exception:
        return 0.0


def _clip_for_shazam(src: str, dst: str, offset: float = 0.0) -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(max(0.0, offset)), "-i", src, "-t", "12", "-vn",
             "-acodec", "libmp3lame", "-b:a", "128k", "-ac", "1", "-ar", "44100", dst],
            check=True, capture_output=True, timeout=90,
        )
        return os.path.exists(dst) and os.path.getsize(dst) > 0
    except Exception as exc:
        log.error("shazam clip error: %s", exc)
        return False


_SHZ_TOTAL_BUDGET = 35.0
_SHZ_PER_CALL_TIMEOUT = 12.0


def _shazam_multi(media_path: str, tmpdir: str):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
    deadline = time.monotonic() + _SHZ_TOTAL_BUDGET
    dur = _media_duration(media_path)
    offsets = [0.0]
    if dur > 20:
        offsets.append(max(0.0, dur / 2 - 6))
    if dur > 45:
        offsets.append(max(0.0, dur * 0.75 - 6))
    retried = False
    pool = ThreadPoolExecutor(max_workers=len(offsets) + 1)
    try:
        for i, off in enumerate(offsets):
            if time.monotonic() >= deadline:
                break
            clip = os.path.join(tmpdir, f"clip{i}.mp3")
            if not _clip_for_shazam(media_path, clip, off):
                continue
            attempts = 2 if not retried else 1
            for _ in range(attempts):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    fut = pool.submit(_shazam_recognize, clip)
                    out = fut.result(timeout=min(_SHZ_PER_CALL_TIMEOUT, remaining))
                    track = (out or {}).get("track")
                    if track:
                        return track
                    break
                except FutTimeout:
                    break
                except Exception as exc:
                    log.error("shazam recognize error: %s", exc)
                    if retried:
                        break
                    retried = True
                    time.sleep(1.0)
    finally:
        pool.shutdown(wait=False)
    return None


def shazam_keyboard(video_id: str, user_id: int) -> types.InlineKeyboardMarkup:
    lock = "" if is_premium(user_id) else " 🔒"
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton(t(user_id, "shz_download"), callback_data=f"shz:{video_id}"))
    kb.add(
        types.InlineKeyboardButton(f"🎤 Vocal Remover{lock}", callback_data=f"eff:vocal:{video_id}"),
        types.InlineKeyboardButton(f"🌀 Audio 8D{lock}", callback_data=f"eff:8d:{video_id}"),
    )
    kb.add(types.InlineKeyboardButton("📜 Testo / Lyrics", callback_data=f"lyr:{video_id}"))
    return kb


def _recognize_and_reply(chat_id: int, uid: int, media_path: str, tmpdir: str,
                         fallback_url: str | None = None) -> None:
    status_id = None
    try:
        try:
            status_id = bot.send_message(chat_id, t(uid, "shz_analyzing")).message_id
        except Exception:
            pass
        track = _shazam_multi(media_path, tmpdir)
        if status_id:
            try:
                bot.delete_message(chat_id, status_id)
            except Exception:
                pass
        if not track:
            if fallback_url:
                start_download(chat_id, fallback_url, uid)
            else:
                bot.send_message(chat_id, t(uid, "shz_not_found"))
            return
        title = track.get("title") or "?"
        artist = track.get("subtitle") or "?"
        cover = (track.get("images") or {}).get("coverart")
        caption = t(uid, "shz_found").format(title=esc(title), artist=esc(artist))
        video_id = ""
        try:
            results = search_youtube(f"{artist} {title}", max_results=1)
            if results and _VIDEO_ID_RE.fullmatch(results[0].get("id", "")):
                video_id = results[0]["id"]
        except Exception as exc:
            log.error("shazam yt lookup error: %s", exc)
        kb = shazam_keyboard(video_id, uid) if video_id else None
        try:
            if cover:
                bot.send_photo(chat_id, cover, caption=caption, reply_markup=kb)
            else:
                bot.send_message(chat_id, caption, reply_markup=kb)
        except Exception:
            bot.send_message(chat_id, caption, reply_markup=kb)
    except Exception as exc:
        log.error("shazam flow crashed: %s", exc)
        try:
            bot.send_message(chat_id, t(uid, "shz_not_found"))
        except Exception:
            pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@bot.message_handler(content_types=["video", "video_note", "voice", "audio"])
def handle_media_recognition(message: types.Message):
    uid = message.from_user.id
    if not message_fresh(message) or not flood_ok(uid):
        return
    upsert_user(uid, message.from_user.username)
    media = message.video or message.video_note or message.voice or message.audio
    if media is None:
        return
    if getattr(media, "file_size", 0) and media.file_size > _SHZ_MAX_FILE:
        bot.send_message(message.chat.id, t(uid, "shz_not_found"))
        return

    def worker():
        tmpdir = tempfile.mkdtemp(prefix="shazam_")
        try:
            tg_file = bot.get_file(media.file_id)
            data = bot.download_file(tg_file.file_path)
            src = os.path.join(tmpdir, "input" + (Path(tg_file.file_path).suffix or ".bin"))
            with open(src, "wb") as fh:
                fh.write(data)
        except Exception as exc:
            log.error("shazam media download error: %s", exc)
            shutil.rmtree(tmpdir, ignore_errors=True)
            try:
                bot.send_message(message.chat.id, t(uid, "shz_not_found"))
            except Exception:
                pass
            return
        _recognize_and_reply(message.chat.id, uid, src, tmpdir)

    threading.Thread(target=worker, daemon=True).start()


def _recognize_from_link(chat_id: int, uid: int, url: str) -> None:
    tmpdir = tempfile.mkdtemp(prefix="shazam_")
    src = os.path.join(tmpdir, "linkaudio.m4a")
    try:
        opts = {
            **_ydl_opts_base(),
            "format": "bestaudio/best",
            "outtmpl": src,
            "noplaylist": True,
            "match_filter": yt_dlp.utils.match_filter_func("duration < 600"),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        log.error("shazam link download error: %s", exc)
        shutil.rmtree(tmpdir, ignore_errors=True)
        start_download(chat_id, url, uid)
        return
    _recognize_and_reply(chat_id, uid, src, tmpdir, fallback_url=url)


@bot.callback_query_handler(func=lambda c: c.data.startswith("shz:"))
def cb_shazam_download(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
        video_id = call.data.split(":", 1)[1]
        if not _VIDEO_ID_RE.match(video_id):
            return
        chat_id = call.message.chat.id if call.message else call.from_user.id
        start_download(chat_id, f"https://www.youtube.com/watch?v={video_id}",
                       call.from_user.id, video_id)
    except Exception as exc:
        log.error("cb_shazam_download error: %s", exc)


# ---------------------------------------------------------------------------
# Audio delivery
# ---------------------------------------------------------------------------

def start_download(chat_id: int, video_url: str, user_id: int, video_id: str = "",
                   fallback_query: str = "") -> None:
    with _active_lock:
        if user_id in active_downloads:
            return
        active_downloads.add(user_id)
    if not check_daily_limit(user_id):
        with _active_lock:
            active_downloads.discard(user_id)
        try:
            bot.send_message(chat_id, pxt(user_id, "limit_reached"))
            send_subscribe_panel(chat_id, user_id)
        except Exception as exc:
            log.error("limit message error: %s", exc)
        return

    def job():
        try:
            send_audio_track(chat_id, video_url, user_id, video_id, fallback_query)
        except Exception as exc:
            log.error("Download job crashed: %s", exc)
        finally:
            with _active_lock:
                active_downloads.discard(user_id)

    download_pool.submit(job)


def _fetch_first_thumbnail(urls: list[str], budget: float = 3.0) -> bytes | None:
    if not urls:
        return None

    def _one(u: str) -> bytes | None:
        resp = requests.get(u, timeout=budget)
        if resp.ok and len(resp.content) > 1000:
            return resp.content
        return None

    pool = ThreadPoolExecutor(max_workers=len(urls))
    futures = {pool.submit(_one, u): i for i, u in enumerate(urls)}
    results: dict[int, bytes] = {}
    deadline = time.time() + budget
    try:
        for fut in as_completed(futures, timeout=budget):
            try:
                data = fut.result()
            except Exception:
                continue
            if data:
                idx = futures[fut]
                results[idx] = data
                if idx == 0:
                    break
                if time.time() >= deadline:
                    break
    except Exception:
        pass
    finally:
        pool.shutdown(wait=False)
    return results[min(results)] if results else None


def send_audio_track(chat_id: int, video_url: str, user_id: int, video_id: str = "",
                     fallback_query: str = "") -> None:
    is_private = chat_id > 0
    cached = get_cached_file_id(video_id) if video_id else None
    if cached:
        bot.send_audio(
            chat_id, audio=cached["file_id"], title=cached["title"],
            performer=cached["performer"], duration=cached["duration"],
            reply_markup=track_keyboard(video_id, user_id) if is_private else None,
        )
        bump_stat("songs_delivered")
        return

    status_msg = bot.send_message(chat_id, t(user_id, "downloading"))
    mp3_path, meta = download_audio(video_url)
    if (
        not mp3_path
        and meta.get("error") != "too_long"
        and fallback_query
        and "soundcloud.com" not in video_url
    ):
        log.info("Download fallback via SoundCloud for: %s", fallback_query)
        mp3_path, meta = download_audio(f"scsearch1:{fallback_query}")
    if mp3_path and video_id:
        meta["video_id"] = video_id
    if not mp3_path:
        err_key = "too_long" if meta.get("error") == "too_long" else "not_found"
        bot.edit_message_text(t(user_id, err_key), chat_id, status_msg.message_id)
        return

    try:
        bot.edit_message_text(t(user_id, "sending"), chat_id, status_msg.message_id)
    except Exception:
        pass

    try:
        thumb_urls = []
        if meta.get("thumbnail"):
            thumb_urls.append(meta["thumbnail"])
        vid = meta.get("video_id", "")
        if len(vid) == 11:
            thumb_urls += [
                f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            ]
        thumb_bytes = _fetch_first_thumbnail(thumb_urls, budget=3.0)

        with open(mp3_path, "rb") as audio_file:
            sent = bot.send_audio(
                chat_id, audio=audio_file, title=meta["title"],
                performer=meta["performer"], duration=meta["duration"],
                thumbnail=thumb_bytes,
                reply_markup=track_keyboard(meta.get("video_id", ""), user_id) if is_private else None,
            )
        if meta.get("video_id"):
            save_download(meta["video_id"], sent.audio.file_id, meta["title"], meta["performer"], meta["duration"])
        bump_stat("songs_delivered")
    except Exception as exc:
        log.error("Send audio error: %s", exc)
        bot.send_message(chat_id, t(user_id, "not_found"))
    finally:
        if not _fx_cache_seed(meta.get("video_id", ""), mp3_path, meta):
            shutil.rmtree(os.path.dirname(mp3_path), ignore_errors=True)
        try:
            bot.delete_message(chat_id, status_msg.message_id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

SUPPORT_TEXTS = {
    "it": (
        "🆘 <b>Serve aiuto?</b>\n\n"
        "• 🔍 <b>Cercare musica:</b> scrivi il titolo o l'artista qui in chat\n"
        "• 🔗 <b>Da link:</b> incolla un link YouTube, Spotify o SoundCloud\n"
        "• 🎧 <b>Riconoscere un brano:</b> invia un vocale, un video o un TikTok\n"
        "• 📜 <b>Testi:</b> premi il pulsante <i>Lyrics</i> sotto ogni canzone\n"
        "• 💎 <b>Stato account:</b> /status — ⭐ <b>Premium:</b> /subscribe\n\n"
        "❓ <b>Problemi o domande?</b> Premi il pulsante qui sotto 👇"
    ),
    "en": (
        "🆘 <b>Need help?</b>\n\n"
        "• 🔍 <b>Search music:</b> type a song title or artist here in chat\n"
        "• 🔗 <b>From a link:</b> paste a YouTube, Spotify or SoundCloud link\n"
        "• 🎧 <b>Recognize a song:</b> send a voice note, video or TikTok\n"
        "• 📜 <b>Lyrics:</b> tap the <i>Lyrics</i> button under any track\n"
        "• 💎 <b>Account status:</b> /status — ⭐ <b>Premium:</b> /subscribe\n\n"
        "❓ <b>Problems or questions?</b> Tap the button below 👇"
    ),
}


def support_text(uid: int) -> str:
    return SUPPORT_TEXTS.get(get_user_lang(uid), SUPPORT_TEXTS["en"])


@bot.message_handler(commands=["help"])
def cmd_help(message: types.Message):
    if not command_ok(message):
        return
    uid = message.from_user.id
    upsert_user(uid, message.from_user.username)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🆘 Supporto / Support", callback_data="sup"))
    bot.send_message(message.chat.id, support_text(uid), reply_markup=markup)


# ---------------------------------------------------------------------------
# FIX 2: /start — video promo + support button restored
# ---------------------------------------------------------------------------

PROMO_VIDEO_PATH = os.path.join(os.path.dirname(__file__), "assets", "promo_start.mp4")
_promo_video_file_id: str | None = None
_promo_video_lock = threading.Lock()


def _send_promo_video(chat_id: int) -> None:
    """Send the promo video on /start. Silently skips if file is missing.
    After the first upload, uses the cached Telegram file_id for instant delivery."""
    global _promo_video_file_id
    try:
        if _promo_video_file_id:
            bot.send_video(chat_id, _promo_video_file_id, width=1280, height=720)
            return
        with _promo_video_lock:
            if _promo_video_file_id:
                bot.send_video(chat_id, _promo_video_file_id, width=1280, height=720)
                return
            if not os.path.exists(PROMO_VIDEO_PATH):
                log.info("Promo video not found at %s — skipping", PROMO_VIDEO_PATH)
                return
            with open(PROMO_VIDEO_PATH, "rb") as f:
                msg = bot.send_video(chat_id, f, width=1280, height=720, supports_streaming=True)
            if msg.video:
                _promo_video_file_id = msg.video.file_id
                log.info("Promo video uploaded, file_id cached")
    except Exception as e:
        log.warning("Failed to send promo video: %s", e)


def start_keyboard(uid: int) -> types.InlineKeyboardMarkup:
    """FIX 3: Support button always present in the /start keyboard."""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(t(uid, "btn_go_premium"), callback_data="sub"))
    markup.add(
        types.InlineKeyboardButton(t(uid, "btn_offline"), callback_data="offhelp"),
        types.InlineKeyboardButton(t(uid, "btn_lang"), callback_data="langmenu"),
    )
    markup.add(types.InlineKeyboardButton("🆘 Supporto / Support", callback_data="sup"))
    return markup


# ---------------------------------------------------------------------------
# In-bot support relay
# ---------------------------------------------------------------------------

_support_pending: set[int] = set()
_support_threads: dict[int, int] = {}
_support_lock = threading.Lock()

SUPPORT_PROMPTS = {
    "it": "🆘 <b>Assistenza</b>\n\nScrivimi ora il tuo messaggio: lo inoltro subito all'assistenza e riceverai la risposta direttamente qui in chat. ✍️",
    "en": "🆘 <b>Support</b>\n\nType your message now: I'll forward it to support and you'll get the answer right here in this chat. ✍️",
}
SUPPORT_SENT = {
    "it": "✅ Messaggio inviato all'assistenza! Riceverai la risposta qui appena possibile. 🙌",
    "en": "✅ Message sent to support! You'll get a reply here as soon as possible. 🙌",
}


def _support_txt(uid: int, table: dict) -> str:
    return table.get(get_user_lang(uid), table["en"])


@bot.callback_query_handler(func=lambda c: c.data == "sup")
def cb_support(call: types.CallbackQuery):
    uid = call.from_user.id
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    if call.message:
        with _support_lock:
            _support_pending.add(uid)
        bot.send_message(call.message.chat.id, _support_txt(uid, SUPPORT_PROMPTS))


@bot.message_handler(commands=["support", "assistenza"])
def cmd_support(message: types.Message):
    if not command_ok(message):
        return
    uid = message.from_user.id
    with _support_lock:
        _support_pending.add(uid)
    bot.send_message(message.chat.id, _support_txt(uid, SUPPORT_PROMPTS))


def _relay_support_message(message: types.Message) -> bool:
    uid = message.from_user.id
    with _support_lock:
        if uid not in _support_pending:
            return False
        _support_pending.discard(uid)
    try:
        uname = f"@{message.from_user.username}" if message.from_user.username else "(no username)"
        header = (f"🆘 <b>Richiesta assistenza</b>\nDa: {esc(message.from_user.first_name or '?')} {esc(uname)}\n"
                  f"ID: <code>{uid}</code>\n\n{esc(message.text or '')}")
        relayed = bot.send_message(ADMIN_ID, header)
        with _support_lock:
            _support_threads[relayed.message_id] = message.chat.id
        bot.send_message(message.chat.id, _support_txt(uid, SUPPORT_SENT))
    except Exception as exc:
        log.error("support relay error: %s", exc)
    return True


_SUPPORT_ID_RE = re.compile(r"ID: (\d+)")


def _relay_admin_reply(message: types.Message) -> bool:
    if message.from_user.id != ADMIN_ID or not message.reply_to_message:
        return False
    with _support_lock:
        target = _support_threads.get(message.reply_to_message.message_id)
    if not target:
        replied_text = message.reply_to_message.text or ""
        if replied_text.startswith("🆘"):
            m = _SUPPORT_ID_RE.search(replied_text)
            if m:
                target = int(m.group(1))
    if not target:
        return False
    try:
        bot.send_message(target, f"💬 <b>Risposta dall'assistenza / Support reply:</b>\n\n{esc(message.text or '')}")
        bot.send_message(ADMIN_ID, "✅ Risposta consegnata all'utente.")
    except Exception as exc:
        log.error("support reply relay error: %s", exc)
    return True


def _language_menu_markup() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(*[
        types.InlineKeyboardButton(label, callback_data=f"lang:{code}")
        for code, label in LANG_FLAGS.items()
    ])
    return markup


@bot.callback_query_handler(func=lambda c: c.data == "offhelp")
def cb_offline_help(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id, t(call.from_user.id, "offline_help"), show_alert=True)
    except Exception as exc:
        log.error("offhelp error: %s", exc)


@bot.callback_query_handler(func=lambda c: c.data == "langmenu")
def cb_lang_menu(call: types.CallbackQuery):
    try:
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, t(call.from_user.id, "choose_lang"),
                         reply_markup=_language_menu_markup())
    except Exception as exc:
        log.error("langmenu error: %s", exc)


@bot.message_handler(commands=["language"])
def cmd_language(message: types.Message):
    if not command_ok(message):
        return
    upsert_user(message.from_user.id, message.from_user.username)
    bot.send_message(message.chat.id, t(message.from_user.id, "choose_lang"),
                     reply_markup=_language_menu_markup())


@bot.message_handler(commands=["admin"])
def cmd_admin(message: types.Message):
    uid = message.from_user.id
    if uid != ADMIN_ID:
        bot.send_message(message.chat.id, t(uid, "admin_denied"))
        return
    stats = get_admin_stats()
    bot.send_message(
        message.chat.id,
        f"{t(uid, 'admin_header')}\n\n"
        f"👥 <b>Total Users:</b> {stats['total_users']}\n"
        f"⭐ <b>Premium:</b> {stats['premium_users']}\n"
        f"📅 <b>Monthly Active:</b> {stats['monthly_active']}\n"
        f"🎵 <b>Songs Delivered:</b> {stats['total_downloads']}",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("lang:"))
def cb_language(call: types.CallbackQuery):
    lang_code = call.data.split(":")[1]
    uid = call.from_user.id
    upsert_user(uid, call.from_user.username)
    if lang_code in LANGUAGES:
        set_user_lang(uid, lang_code)
        bot.answer_callback_query(call.id, LANGUAGES[lang_code]["lang_set"])
        try:
            bot.edit_message_text(LANGUAGES[lang_code]["lang_set"], call.message.chat.id, call.message.message_id)
        except Exception:
            pass
    else:
        bot.answer_callback_query(call.id)


# ---------------------------------------------------------------------------
# Navigation buttons
# ---------------------------------------------------------------------------

@bot.message_handler(func=lambda m: m.text in (BTN_PREV, BTN_NEXT, BTN_CLOSE))
def handle_navigation(message: types.Message):
    if not message_fresh(message):
        return
    uid = message.from_user.id
    state = user_search_cache.get(uid)

    if message.text == BTN_CLOSE:
        user_search_cache.pop(uid, None)
        bot.send_message(message.chat.id, t(uid, "closed"), reply_markup=types.ReplyKeyboardRemove())
        return

    if not state:
        bot.send_message(message.chat.id, t(uid, "no_search"), reply_markup=types.ReplyKeyboardRemove())
        return

    if message.text == BTN_NEXT:
        if state["offset"] + PAGE_SIZE < len(state["results"]):
            state["offset"] += PAGE_SIZE
            send_results_page(message.chat.id, uid)
    elif message.text == BTN_PREV:
        if state["offset"] - PAGE_SIZE >= 0:
            state["offset"] -= PAGE_SIZE
            send_results_page(message.chat.id, uid)


# ---------------------------------------------------------------------------
# Track selection (numbers 1-10)
# ---------------------------------------------------------------------------

@bot.message_handler(func=lambda m: m.text and m.text.strip().isdigit() and 1 <= int(m.text.strip()) <= 10)
def handle_track_selection(message: types.Message):
    uid = message.from_user.id
    if not message_fresh(message) or not flood_ok(uid):
        return
    state = user_search_cache.get(uid)
    if not state:
        bot.send_message(message.chat.id, t(uid, "no_search"), reply_markup=types.ReplyKeyboardRemove())
        return

    idx = state["offset"] + int(message.text.strip()) - 1
    if idx >= len(state["results"]):
        bot.send_message(message.chat.id, t(uid, "invalid"))
        return

    track = state["results"][idx]
    if track.get("duration") and track["duration"] > MAX_TRACK_SECONDS:
        bot.send_message(message.chat.id, t(uid, "too_long"))
        return
    start_download(message.chat.id, track["url"], uid, track["id"],
                   fallback_query=clean_title(track["title"], track["uploader"]))


# ---------------------------------------------------------------------------
# URL handler
# ---------------------------------------------------------------------------

MAX_PLAYLIST_TRACKS = 10


def _download_playlist(chat_id: int, user_id: int, url: str) -> None:
    try:
        opts = {
            **_ydl_opts_base(),
            "extract_flat": True,
            "playlistend": MAX_PLAYLIST_TRACKS,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            bot.send_message(chat_id, t(user_id, "not_found"))
            return
        bot.send_message(chat_id, f"📂 {len(entries)} 🎵 ⏳")
        for e in entries:
            vid = e.get("id", "")
            vurl = e.get("url") or f"https://www.youtube.com/watch?v={vid}"
            try:
                send_audio_track(chat_id, vurl, user_id, vid)
            except Exception as exc:
                log.error("Playlist track failed (%s): %s", vid, exc)
    except Exception as exc:
        log.error("Playlist download failed: %s", exc)
        try:
            bot.send_message(chat_id, t(user_id, "not_found"))
        except Exception:
            pass


def _is_spotify_url(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        u = url if "://" in url else f"https://{url}"
        return urlparse(u).netloc.lower() == "open.spotify.com"
    except Exception:
        return False


def _spotify_track_query(url: str) -> str | None:
    if not _is_spotify_url(url):
        return None
    if "://" not in url:
        url = f"https://{url}"
    try:
        r = requests.get(url.split("?")[0], timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.ok:
            m = re.search(r"<title>(.*?)</title>", r.text)
            if m:
                raw = html.unescape(m.group(1)).replace("| Spotify", "").strip()
                m2 = re.match(r"(.*?)\s+-\s+song(?: and lyrics)? by\s+(.*)", raw)
                if m2:
                    return f"{m2.group(2).strip()} {m2.group(1).strip()}"
                if raw and raw.lower() != "spotify":
                    return raw
        r = requests.get("https://open.spotify.com/oembed", params={"url": url}, timeout=8)
        if r.ok:
            return (r.json().get("title") or "").strip() or None
    except Exception as exc:
        log.warning("Spotify title lookup failed: %s", exc)
    return None


@bot.message_handler(func=lambda m: m.text and URL_PATTERNS.search(m.text))
def handle_url(message: types.Message):
    uid = message.from_user.id
    if _relay_admin_reply(message) or _relay_support_message(message):
        return
    if not message_fresh(message) or not flood_ok(uid):
        return
    upsert_user(uid, message.from_user.username)
    url = message.text.strip()
    if _PLAYLIST_RE.search(url):
        if not is_premium(uid):
            bot.send_message(message.chat.id, pxt(uid, "playlist_premium"))
            send_subscribe_panel(message.chat.id, uid)
            return
        bot.send_message(message.chat.id, t(uid, "url_processing"))
        threading.Thread(target=_download_playlist, args=(message.chat.id, uid, url), daemon=True).start()
        return
    if _SHORTVID_RE.search(url):
        threading.Thread(target=_recognize_from_link, args=(message.chat.id, uid, url), daemon=True).start()
        return
    bot.send_message(message.chat.id, t(uid, "url_processing"))
    if _is_spotify_url(url):
        query = _spotify_track_query(url)
        if not query:
            bot.send_message(message.chat.id, t(uid, "not_found"))
            return
        try:
            tracks = search_youtube(query, max_results=1)
        except Exception as exc:
            log.error("Spotify music lookup failed: %s", exc)
            tracks = []
        if not tracks:
            bot.send_message(message.chat.id, t(uid, "not_found"))
            return
        track = tracks[0]
        start_download(
            message.chat.id,
            track["url"],
            uid,
            track["id"],
            fallback_query=clean_title(track["title"], track["uploader"]),
        )
        return
    yt_match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    video_id = yt_match.group(1) if yt_match else ""
    start_download(message.chat.id, url, uid, video_id)


# ---------------------------------------------------------------------------
# /start handler
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start"])
def cmd_start(message: types.Message):
    if not command_ok(message):
        return
    uid = message.from_user.id
    upsert_user(uid, message.from_user.username)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("dl_"):
        video_id = parts[1][3:]
        if len(video_id) == 11:
            start_download(message.chat.id, f"https://www.youtube.com/watch?v={video_id}", uid, video_id)
            return
    if len(parts) == 2 and parts[1].startswith("lyr_"):
        video_id = parts[1][4:]
        def worker():
            track = get_cached_file_id(video_id)
            text = fetch_lyrics(track["title"], track["performer"]) if track else None
            if not text and not track:
                info = get_audio_stream_url(f"https://www.youtube.com/watch?v={video_id}")
                if info:
                    text = fetch_lyrics(info["title"], info["uploader"])
                    track = {"title": info["title"], "performer": info["uploader"]}
            if not text:
                bot.send_message(message.chat.id, t(uid, "lyrics_not_found"))
                return
            header = f"📝 <b>{esc(clean_title(track['title'], track['performer']))}</b>\n\n"
            bot.send_message(message.chat.id, header + esc(text[:3900] + ("…" if len(text) > 3900 else "")))
        threading.Thread(target=worker, daemon=True).start()
        return
    # Send promo video (skipped silently if file missing)
    _send_promo_video(message.chat.id)
    # Send welcome message with all buttons including Support
    bot.send_message(message.chat.id, t(uid, "welcome"), reply_markup=start_keyboard(uid))


# ---------------------------------------------------------------------------
# Text search
# ---------------------------------------------------------------------------

@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
def handle_search(message: types.Message):
    uid = message.from_user.id
    if _relay_admin_reply(message) or _relay_support_message(message):
        return
    if not message_fresh(message) or not flood_ok(uid):
        return
    upsert_user(uid, message.from_user.username)
    query = message.text.strip()

    status_msg = bot.send_message(message.chat.id, t(uid, "searching"))
    try:
        results = search_youtube(query)
    except Exception as exc:
        log.error("Search error: %s", exc)
        results = []

    try:
        bot.delete_message(message.chat.id, status_msg.message_id)
    except Exception:
        pass

    if not results:
        bot.send_message(message.chat.id, t(uid, "not_found"))
        return

    user_search_cache[uid] = {"results": results, "offset": 0, "query": query, "ts": time.time()}
    send_results_page(message.chat.id, uid)


# ---------------------------------------------------------------------------
# Command panel
# ---------------------------------------------------------------------------

def setup_command_panel() -> None:
    default_cmds = [
        types.BotCommand("start", LANGUAGES["en"]["cmd_start"]),
        types.BotCommand("language", LANGUAGES["en"]["cmd_language"]),
        types.BotCommand("help", LANGUAGES["en"]["cmd_help"]),
        types.BotCommand("subscribe", "⭐ Premium (Telegram Stars)"),
        types.BotCommand("status", "💎 Account status"),
    ]
    try:
        bot.set_my_commands(default_cmds)
    except Exception as exc:
        log.warning("set_my_commands failed: %s", exc)
    for code, strings in LANGUAGES.items():
        if code == "en":
            continue
        try:
            bot.set_my_commands(
                [
                    types.BotCommand("start", strings["cmd_start"]),
                    types.BotCommand("language", strings["cmd_language"]),
                    types.BotCommand("help", strings["cmd_help"]),
                    types.BotCommand("subscribe", "⭐ Premium (Telegram Stars)"),
                    types.BotCommand("status", "💎 Account status"),
                ],
                language_code=code,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN environment variable is not set.")

    init_db()
    setup_command_panel()
    _setup_node_path()
    _setup_yt_cookies()

    for _attempt in range(6):
        try:
            bot.delete_webhook(drop_pending_updates=False)
            log.info("Webhook cleared (attempt %d)", _attempt + 1)
            break
        except Exception as _e:
            log.warning("delete_webhook failed (%d/6): %s — retrying in 5s", _attempt + 1, _e)
            time.sleep(5)

    threading.Thread(target=_auto_cleaner, daemon=True, name="auto-cleaner").start()

    try:
        _conn = get_db()
        for _row in _conn.execute("SELECT user_id FROM users WHERE is_premium = 1").fetchall():
            _premium_lkg[_row["user_id"]] = True
        log.info("Premium cache pre-warmed: %d users", len(_premium_lkg))
    except Exception as _exc:
        log.error("premium cache prewarm failed: %s", _exc)

    def _prewarm_ytdlp():
        """Solve YouTube JS challenge at startup so the first user doesn't wait."""
        try:
            _t = time.time()
            opts = {
                **_ydl_opts_base(),
                "skip_download": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ", download=False)
            log.info("yt-dlp prewarm done in %.1fs", time.time() - _t)
        except Exception as exc:
            log.warning("yt-dlp prewarm failed (non-fatal): %s", exc)

    threading.Thread(target=_prewarm_ytdlp, daemon=True, name="yt-prewarm").start()
    log.info("Bot polling started...")
    while True:
        try:
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=30,
                logger_level=logging.WARNING,
                allowed_updates=["message", "callback_query", "inline_query", "chosen_inline_result"],
                skip_pending=False,
            )
        except Exception as exc:
            log.error("Polling crashed, restarting in 10s: %s", exc)
            time.sleep(10)
            try:
                bot.delete_webhook(drop_pending_updates=False)
            except Exception:
                pass
