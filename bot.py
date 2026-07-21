#!/usr/bin/env python3
"""
Studio-quality multi-language Telegram Music Bot
Dual search modes, 11 languages, audiophile EQ, cover art,
SQLite caching, admin panel. Optimized for Railway / mobile use.
"""

import html
import os
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID = 8584283379
DB_PATH = os.path.join(os.path.dirname(__file__), "bot_data.db")
PAGE_SIZE = 10
SEARCH_POOL = 30  # fetch 30 results once, page through them 10 at a time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=True, num_threads=8)

# In-memory state
# uid → {"results": [...], "offset": int}
user_search_cache: dict[int, dict] = {}
# uid → language code (avoids a DB hit on every message)
lang_cache: dict[int, str] = {}

BTN_PREV, BTN_CLOSE, BTN_NEXT = "⬅️", "❌", "➡️"

# ---------------------------------------------------------------------------
# Anti-flood, bounded concurrency & auto-cleaner
# ---------------------------------------------------------------------------

# Global bounded pool: max 4 simultaneous downloads keeps CPU/RAM stable even
# with many users; the rest queue up instead of crashing the bot.
download_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dl")

# uid → set of chats with a download in progress (1 concurrent download per user)
active_downloads: set[int] = set()
_active_lock = threading.Lock()

# uid → [timestamps of recent actions] for the anti-flood window
_flood: dict[int, list[float]] = {}
_flood_lock = threading.Lock()
FLOOD_MIN_INTERVAL = 1.0    # seconds between messages
FLOOD_MAX_PER_MIN = 20      # max actions per rolling minute

# Zero-width / control chars that make titles invisible or break formatting
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00ad\x00-\x08\x0b\x0c\x0e-\x1f]")


def flood_ok(uid: int) -> bool:
    """True if the user is within rate limits; False = silently ignore."""
    now = time.monotonic()
    with _flood_lock:
        stamps = _flood.setdefault(uid, [])
        # drop entries older than 60s
        while stamps and now - stamps[0] > 60:
            stamps.pop(0)
        if stamps and now - stamps[-1] < FLOOD_MIN_INTERVAL:
            return False
        if len(stamps) >= FLOOD_MAX_PER_MIN:
            return False
        stamps.append(now)
        return True


def esc(text: str) -> str:
    """Sanitize any external text before HTML messages: strip invisible chars, escape HTML."""
    return html.escape(_INVISIBLE_RE.sub("", text or "").strip())


def _auto_cleaner() -> None:
    """Background janitor: frees memory & disk, never touches user data in the DB."""
    while True:
        time.sleep(600)  # every 10 minutes
        try:
            now = time.time()
            # 1) Expire search sessions older than 2h
            for uid in list(user_search_cache):
                state = user_search_cache.get(uid)
                if state and now - state.get("ts", now) > 7200:
                    user_search_cache.pop(uid, None)
            # 2) Cap in-memory caches
            if len(lang_cache) > 5000:
                lang_cache.clear()
            with _flood_lock:
                for uid in list(_flood):
                    if not _flood[uid] or time.monotonic() - _flood[uid][-1] > 300:
                        _flood.pop(uid, None)
            # 3) Remove orphaned download temp dirs older than 1h
            tmp_root = tempfile.gettempdir()
            for entry in Path(tmp_root).glob("musicbot_*"):
                try:
                    if entry.is_dir() and now - entry.stat().st_mtime > 3600:
                        shutil.rmtree(entry, ignore_errors=True)
                except Exception:
                    pass
            # 4) Compact the SQLite WAL so the DB file never balloons
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
        "searching": "🔍 Cerco su YouTube...",
        "downloading": "⬇️ Scarico la traccia...",
        "sending": "📤 Invio in corso...",
        "not_found": "❌ Nessun risultato trovato.",
        "too_long": "❌ Traccia troppo lunga (max 20 min).",
        "lyrics_not_found": "❌ Testo non trovato.",
        "choose_lang": "🌍 Scegli la tua lingua:",
        "lang_set": "✅ Lingua impostata su Italiano 🇮🇹",
        "invalid": "⚠️ Numero non valido.",
        "no_search": "⚠️ Prima cerca una canzone.",
        "cached": "⚡ Invio dalla cache...",
        "welcome": "👋 <b>Benvenuto su Music Stream HQ!</b> 🎧\n<i>Il tuo hub personale per cercare, riconoscere e scaricare musica ad altissima fedeltà.</i>\n\n━━━━━━━━━━━━━━\n🆓 <b>PIANO GRATUITO (FREE):</b>\n• 🎧 <b>Shazam Integrato:</b> Invia un video TikTok, un Reel o un vocale per scoprire al volo il titolo della canzone! 🔍\n• 🎵 <b>8 Download al giorno</b> (MP3 320kbps in alta qualità)\n• 🔍 <b>Ricerca Intelligente:</b> Cerca per Titolo, Artista o Link (YouTube, Spotify, SoundCloud)\n• 📜 <b>Testi delle canzoni</b> (<i>Lyrics</i>) gratuiti e sbloccati per tutti\n• ✈️ <b>Ascolto Offline:</b> Salva le canzoni e ascoltale anche in Modalità Aereo!\n\n⭐ <b>FUNZIONALITÀ PREMIUM (SBLOCCABILI):</b>\n• ♾️ <b>Download Illimitati</b> — Zero limiti giornalieri e zero attese\n• 📂 <b>Download Playlist &amp; Album</b> — Scarica raccolte intere in 1 solo click\n• 💎 <b>Qualità FLAC / Lossless</b> — Audio puro da studio senza compressione\n• 🎤 <b>Karaoke / Vocal Remover</b> — Separa ed elimina la voce dalla base\n• 🌀 <b>Audio 8D &amp; Effetti FX</b> — Suono spaziale a 360°, Bass Boost e Nightcore\n• ⚡ <b>Coda Prioritaria</b> — Download ultra-veloci sui server\n\n━━━━━━━━━━━━━━\n🚀 <i>Pronto ad iniziare? Invia subito un video TikTok, un vocale o scrivi il nome di un brano qui in chat!</i> 👇",
        "offline_help": '✈️ Salva i brani sul telefono dalla chat: una volta scaricati li ascolti anche senza internet o in Modalità Aereo!',
        "btn_go_premium": '⭐ Passa a Premium (Stelle)',
        "btn_offline": '✈️ Come ascoltare Offline?',
        "btn_lang": '🌍 Cambia Lingua',
        "admin_header": "📊 <b>Pannello Admin</b>",
        "admin_denied": "⛔ Accesso negato.",
        "url_processing": "🔗 Processo il link...",
        "closed": "✅ Ricerca chiusa.",
        "cmd_start": "Avvia il bot",
        "cmd_language": "Cambia lingua",
        "cmd_help": "Guida",
    },
    "en": {
        "searching": "🔍 Searching YouTube...",
        "downloading": "⬇️ Downloading track...",
        "sending": "📤 Sending...",
        "not_found": "❌ No results found.",
        "too_long": "❌ Track too long (max 20 min).",
        "lyrics_not_found": "❌ Lyrics not found.",
        "choose_lang": "🌍 Choose your language:",
        "lang_set": "✅ Language set to English 🇬🇧",
        "invalid": "⚠️ Invalid number.",
        "no_search": "⚠️ Search for a song first.",
        "cached": "⚡ Sending from cache...",
        "welcome": "👋 <b>Welcome to Music Stream HQ!</b> 🎧\n<i>Your personal hub to search, recognize, and download music in the highest quality.</i>\n\n━━━━━━━━━━━━━━\n🆓 <b>FREE PLAN:</b>\n• 🎧 <b>Built-in Shazam:</b> Send a TikTok video, a Reel, or a voice note to instantly discover the song! 🔍\n• 🎵 <b>8 Downloads per day</b> (MP3 320kbps high quality)\n• 🔍 <b>Smart Search:</b> Search by title, artist or link (YouTube, Spotify, SoundCloud)\n• 📜 <b>Song Lyrics</b> — free and unlocked for everyone\n• ✈️ <b>Offline Listening:</b> Save songs and listen even in Airplane Mode!\n\n⭐ <b>PREMIUM FEATURES (UNLOCKABLE):</b>\n• ♾️ <b>Unlimited Downloads</b> — Zero daily limits, zero waiting\n• 📂 <b>Playlist &amp; Album Downloads</b> — Full collections in 1 click\n• 💎 <b>FLAC / Lossless Quality</b> — Pure studio audio, no compression\n• 🎤 <b>Karaoke / Vocal Remover</b> — Separate and remove vocals from the beat\n• 🌀 <b>8D Audio &amp; FX Effects</b> — 360° spatial sound, Bass Boost &amp; Nightcore\n• ⚡ <b>Priority Queue</b> — Ultra-fast downloads on our servers\n\n━━━━━━━━━━━━━━\n🚀 <i>Ready to start? Send a TikTok video, a voice note, or type a song name in chat right now!</i> 👇",
        "offline_help": '✈️ Save tracks to your phone from this chat: once downloaded you can listen with no internet or in Airplane Mode!',
        "btn_go_premium": '⭐ Go Premium (Stars)',
        "btn_offline": '✈️ How to listen Offline?',
        "btn_lang": '🌍 Change Language',
        "admin_header": "📊 <b>Admin Panel</b>",
        "admin_denied": "⛔ Access denied.",
        "url_processing": "🔗 Processing link...",
        "closed": "✅ Search closed.",
        "cmd_start": "Start the bot",
        "cmd_language": "Change language",
        "cmd_help": "Help",
    },
    "es": {
        "searching": "🔍 Buscando en YouTube...",
        "downloading": "⬇️ Descargando pista...",
        "sending": "📤 Enviando...",
        "not_found": "❌ No se encontraron resultados.",
        "too_long": "❌ Pista demasiado larga (máx. 20 min).",
        "lyrics_not_found": "❌ Letra no encontrada.",
        "choose_lang": "🌍 Elige tu idioma:",
        "lang_set": "✅ Idioma configurado a Español 🇪🇸",
        "invalid": "⚠️ Número inválido.",
        "no_search": "⚠️ Primero busca una canción.",
        "cached": "⚡ Enviando desde caché...",
        "welcome": "👋 <b>¡Bienvenido a Music Stream HQ!</b> 🎧\n<i>Tu hub personal para buscar, reconocer y descargar música en la más alta fidelidad.</i>\n\n━━━━━━━━━━━━━━\n🆓 <b>PLAN GRATUITO (FREE):</b>\n• 🎧 <b>Shazam Integrado:</b> Envía un video de TikTok, un Reel o una nota de voz para descubrir la canción al instante! 🔍\n• 🎵 <b>8 descargas al día</b> (MP3 320kbps en alta calidad)\n• 🔍 <b>Búsqueda Inteligente:</b> Busca por título, artista o enlace (YouTube, Spotify, SoundCloud)\n• 📜 <b>Letras de canciones</b> — gratis y desbloqueadas para todos\n• ✈️ <b>Escucha Offline:</b> Guarda canciones y escúchalas en Modo Avión!\n\n⭐ <b>FUNCIONES PREMIUM (DESBLOQUEABLES):</b>\n• ♾️ <b>Descargas Ilimitadas</b> — Cero límites diarios y cero esperas\n• 📂 <b>Descargas de Playlists &amp; Álbumes</b> — Colecciones enteras en 1 solo clic\n• 💎 <b>Calidad FLAC / Lossless</b> — Audio puro de estudio sin compresión\n• 🎤 <b>Karaoke / Vocal Remover</b> — Separa y elimina la voz de la base\n• 🌀 <b>Audio 8D &amp; Efectos FX</b> — Sonido espacial 360°, Bass Boost y Nightcore\n• ⚡ <b>Cola Prioritaria</b> — Descargas ultrarrápidas en los servidores\n\n━━━━━━━━━━━━━━\n🚀 <i>Listo para empezar? Envía un video de TikTok, una nota de voz o escribe el nombre de una canción!</i> 👇",
        "offline_help": '✈️ Guarda las pistas en tu teléfono desde este chat: una vez descargadas podrás escucharlas sin internet o en Modo Avión!',
        "btn_go_premium": '⭐ Hazte Premium (Estrellas)',
        "btn_offline": '✈️ ¿Cómo escuchar Offline?',
        "btn_lang": '🌍 Cambiar Idioma',
        "admin_header": "📊 <b>Panel Admin</b>",
        "admin_denied": "⛔ Acceso denegado.",
        "url_processing": "🔗 Procesando enlace...",
        "closed": "✅ Búsqueda cerrada.",
        "cmd_start": "Iniciar el bot",
        "cmd_language": "Cambiar idioma",
        "cmd_help": "Ayuda",
    },
    "fr": {
        "searching": "🔍 Recherche sur YouTube...",
        "downloading": "⬇️ Téléchargement...",
        "sending": "📤 Envoi...",
        "not_found": "❌ Aucun résultat trouvé.",
        "too_long": "❌ Piste trop longue (max 20 min).",
        "lyrics_not_found": "❌ Paroles introuvables.",
        "choose_lang": "🌍 Choisissez votre langue:",
        "lang_set": "✅ Langue définie sur Français 🇫🇷",
        "invalid": "⚠️ Numéro invalide.",
        "no_search": "⚠️ Cherchez d'abord une chanson.",
        "cached": "⚡ Envoi depuis le cache...",
        "welcome": "👋 <b>Bienvenue sur Music Stream HQ !</b> 🎧\n<i>Votre hub personnel pour rechercher, reconnaître et télécharger de la musique en haute fidélité.</i>\n\n━━━━━━━━━━━━━━\n🆓 <b>FORFAIT GRATUIT (FREE) :</b>\n• 🎧 <b>Shazam Intégré :</b> Envoyez une vidéo TikTok, un Reel ou une note vocale pour identifier la chanson à la volée ! 🔍\n• 🎵 <b>8 téléchargements par jour</b> (MP3 320kbps haute qualité)\n• 🔍 <b>Recherche Intelligente :</b> Recherchez par titre, artiste ou lien (YouTube, Spotify, SoundCloud)\n• 📜 <b>Paroles des chansons</b> — gratuites et débloquées pour tous\n• ✈️ <b>Écoute Hors ligne :</b> Sauvegardez vos chansons et écoutez-les même en Mode Avion !\n\n⭐ <b>FONCTIONS PREMIUM (À DÉBLOQUER) :</b>\n• ♾️ <b>Téléchargements Illimités</b> — Zéro limite quotidienne, zéro attente\n• 📂 <b>Téléchargement de Playlists &amp; Albums</b> — Des collections entières en 1 clic\n• 💎 <b>Qualité FLAC / Lossless</b> — Audio pur de studio sans compression\n• 🎤 <b>Karaoké / Vocal Remover</b> — Séparez et supprimez la voix de l instru\n• 🌀 <b>Audio 8D &amp; Effets FX</b> — Son spatial 360°, Bass Boost et Nightcore\n• ⚡ <b>File Prioritaire</b> — Téléchargements ultra-rapides sur nos serveurs\n\n━━━━━━━━━━━━━━\n🚀 <i>Prêt à commencer ? Envoyez une vidéo TikTok, une note vocale ou tapez le nom d une chanson ici !</i> 👇",
        "offline_help": '✈️ Enregistrez les titres sur votre téléphone depuis ce chat : une fois téléchargés, écoutez-les sans internet ou en Mode Avion !',
        "btn_go_premium": '⭐ Passer Premium (Étoiles)',
        "btn_offline": '✈️ Écouter hors ligne ?',
        "btn_lang": '🌍 Changer de langue',
        "admin_header": "📊 <b>Panneau Admin</b>",
        "admin_denied": "⛔ Accès refusé.",
        "url_processing": "🔗 Traitement du lien...",
        "closed": "✅ Recherche fermée.",
        "cmd_start": "Démarrer le bot",
        "cmd_language": "Changer de langue",
        "cmd_help": "Aide",
    },
    "de": {
        "searching": "🔍 YouTube wird durchsucht...",
        "downloading": "⬇️ Herunterladen...",
        "sending": "📤 Senden...",
        "not_found": "❌ Keine Ergebnisse gefunden.",
        "too_long": "❌ Titel zu lang (max. 20 Min.).",
        "lyrics_not_found": "❌ Songtext nicht gefunden.",
        "choose_lang": "🌍 Wählen Sie Ihre Sprache:",
        "lang_set": "✅ Sprache auf Deutsch 🇩🇪 eingestellt",
        "invalid": "⚠️ Ungültige Nummer.",
        "no_search": "⚠️ Suchen Sie zuerst einen Song.",
        "cached": "⚡ Aus Cache senden...",
        "welcome": "👋 <b>Willkommen bei Music Stream HQ!</b> 🎧\n<i>Dein persönlicher Hub zum Suchen, Erkennen und Herunterladen von Musik in höchster Qualität.</i>\n\n━━━━━━━━━━━━━━\n🆓 <b>GRATIS-PLAN (FREE):</b>\n• 🎧 <b>Integriertes Shazam:</b> Schicke ein TikTok-Video, einen Reel oder eine Sprachnachricht, um den Song sofort zu erkennen! 🔍\n• 🎵 <b>8 Downloads pro Tag</b> (MP3 320kbps in hoher Qualität)\n• 🔍 <b>Intelligente Suche:</b> Suche nach Titel, Künstler oder Link (YouTube, Spotify, SoundCloud)\n• 📜 <b>Songtexte</b> — kostenlos und für alle freigeschaltet\n• ✈️ <b>Offline hören:</b> Speichere Songs und höre sie auch im Flugmodus!\n\n⭐ <b>PREMIUM-FUNKTIONEN (FREISCHALTBAR):</b>\n• ♾️ <b>Unbegrenzte Downloads</b> — Null Tageslimits und null Wartezeit\n• 📂 <b>Playlists &amp; Alben herunterladen</b> — Ganze Sammlungen mit 1 Klick\n• 💎 <b>FLAC / Lossless-Qualität</b> — Purer Studioklang ohne Kompression\n• 🎤 <b>Karaoke / Vocal Remover</b> — Trenne und entferne Gesang vom Beat\n• 🌀 <b>8D-Audio &amp; FX-Effekte</b> — 360°-Raumklang, Bass Boost &amp; Nightcore\n• ⚡ <b>Prioritäts-Warteschlange</b> — Ultraschnelle Downloads auf unseren Servern\n\n━━━━━━━━━━━━━━\n🚀 <i>Bereit loszulegen? Schick ein TikTok-Video, eine Sprachnachricht oder schreibe einen Songnamen in den Chat!</i> 👇",
        "offline_help": '✈️ Speichere Songs aus diesem Chat auf deinem Handy: einmal heruntergeladen, hörst du sie ohne Internet oder im Flugmodus!',
        "btn_go_premium": '⭐ Premium holen (Sterne)',
        "btn_offline": '✈️ Offline hören – wie?',
        "btn_lang": '🌍 Sprache ändern',
        "admin_header": "📊 <b>Admin-Panel</b>",
        "admin_denied": "⛔ Zugriff verweigert.",
        "url_processing": "🔗 Link wird verarbeitet...",
        "closed": "✅ Suche geschlossen.",
        "cmd_start": "Bot starten",
        "cmd_language": "Sprache ändern",
        "cmd_help": "Hilfe",
    },
    "pt": {
        "searching": "🔍 Pesquisando no YouTube...",
        "downloading": "⬇️ Baixando faixa...",
        "sending": "📤 Enviando...",
        "not_found": "❌ Nenhum resultado encontrado.",
        "too_long": "❌ Faixa muito longa (máx. 20 min).",
        "lyrics_not_found": "❌ Letra não encontrada.",
        "choose_lang": "🌍 Escolha seu idioma:",
        "lang_set": "✅ Idioma definido para Português 🇵🇹",
        "invalid": "⚠️ Número inválido.",
        "no_search": "⚠️ Procure uma música primeiro.",
        "cached": "⚡ Enviando do cache...",
        "welcome": "👋 <b>Bem-vindo ao Music Stream HQ!</b> 🎧\n<i>Seu hub pessoal para buscar, reconhecer e baixar música em altíssima qualidade.</i>\n\n━━━━━━━━━━━━━━\n🆓 <b>PLANO GRATUITO (FREE):</b>\n• 🎧 <b>Shazam Integrado:</b> Envie um vídeo do TikTok, um Reel ou uma nota de voz para descobrir a música na hora! 🔍\n• 🎵 <b>8 downloads por dia</b> (MP3 320kbps em alta qualidade)\n• 🔍 <b>Pesquisa Inteligente:</b> Pesquise por título, artista ou link (YouTube, Spotify, SoundCloud)\n• 📜 <b>Letras das músicas</b> — gratuitas e desbloqueadas para todos\n• ✈️ <b>Audição Offline:</b> Salve músicas e ouça mesmo no Modo Avião!\n\n⭐ <b>RECURSOS PREMIUM (DESBLOQUEÁVEIS):</b>\n• ♾️ <b>Downloads Ilimitados</b> — Zero limites diários e zero esperas\n• 📂 <b>Download de Playlists &amp; Álbuns</b> — Coleções inteiras em 1 clique\n• 💎 <b>Qualidade FLAC / Lossless</b> — Áudio puro de estúdio sem compressão\n• 🎤 <b>Karaokê / Vocal Remover</b> — Separe e remova vocais da base\n• 🌀 <b>Áudio 8D &amp; Efeitos FX</b> — Som espacial 360°, Bass Boost e Nightcore\n• ⚡ <b>Fila Prioritária</b> — Downloads ultrarrápidos nos servidores\n\n━━━━━━━━━━━━━━\n🚀 <i>Pronto para começar? Envie um vídeo do TikTok, uma nota de voz ou digite o nome de uma música aqui!</i> 👇",
        "offline_help": '✈️ Salve as faixas no celular a partir deste chat: depois de baixadas, ouça sem internet ou no Modo Avião!',
        "btn_go_premium": '⭐ Seja Premium (Estrelas)',
        "btn_offline": '✈️ Como ouvir Offline?',
        "btn_lang": '🌍 Mudar Idioma',
        "admin_header": "📊 <b>Painel Admin</b>",
        "admin_denied": "⛔ Acesso negado.",
        "url_processing": "🔗 Processando link...",
        "closed": "✅ Pesquisa fechada.",
        "cmd_start": "Iniciar o bot",
        "cmd_language": "Mudar idioma",
        "cmd_help": "Ajuda",
    },
    "ru": {
        "searching": "🔍 Поиск на YouTube...",
        "downloading": "⬇️ Скачивание...",
        "sending": "📤 Отправка...",
        "not_found": "❌ Результаты не найдены.",
        "too_long": "❌ Трек слишком длинный (макс. 20 мин).",
        "lyrics_not_found": "❌ Текст не найден.",
        "choose_lang": "🌍 Выберите язык:",
        "lang_set": "✅ Язык установлен на Русский 🇷🇺",
        "invalid": "⚠️ Неверный номер.",
        "no_search": "⚠️ Сначала найдите песню.",
        "cached": "⚡ Отправка из кэша...",
        "welcome": "👋 <b>Добро пожаловать в Music Stream HQ!</b> 🎧\n<i>Ваш личный хаб для поиска, распознавания и скачивания музыки в наивысшем качестве.</i>\n\n━━━━━━━━━━━━━━\n🆓 <b>БЕСПЛАТНЫЙ ПЛАН (FREE):</b>\n• 🎧 <b>Встроенный Shazam:</b> Отправьте видео TikTok, Reel или голосовое сообщение и мгновенно узнайте название песни! 🔍\n• 🎵 <b>8 загрузок в день</b> (MP3 320kbps, высокое качество)\n• 🔍 <b>Умный поиск:</b> По названию, исполнителю или ссылке (YouTube, Spotify, SoundCloud)\n• 📜 <b>Тексты песен</b> — бесплатно и доступно для всех\n• ✈️ <b>Офлайн-прослушивание:</b> Сохраняйте песни и слушайте даже в авиарежиме!\n\n⭐ <b>ПРЕМИУМ-ФУНКЦИИ (РАЗБЛОКИРУЕМЫЕ):</b>\n• ♾️ <b>Безлимитные загрузки</b> — Ноль дневных лимитов и ноль ожидания\n• 📂 <b>Загрузка плейлистов &amp; альбомов</b> — Целые коллекции в 1 клик\n• 💎 <b>Качество FLAC / Lossless</b> — Чистый студийный звук без сжатия\n• 🎤 <b>Караоке / Vocal Remover</b> — Отдели и убери вокал из трека\n• 🌀 <b>8D-звук &amp; FX-эффекты</b> — Объёмный звук 360°, Bass Boost и Nightcore\n• ⚡ <b>Приоритетная очередь</b> — Сверхбыстрые загрузки на серверах\n\n━━━━━━━━━━━━━━\n🚀 <i>Готовы начать? Отправьте видео TikTok, голосовое или напишите название песни прямо сейчас!</i> 👇",
        "offline_help": '✈️ Сохраняйте треки на телефон из этого чата: после скачивания слушайте их без интернета или в авиарежиме!',
        "btn_go_premium": '⭐ Премиум (Звёзды)',
        "btn_offline": '✈️ Как слушать офлайн?',
        "btn_lang": '🌍 Сменить язык',
        "admin_header": "📊 <b>Панель администратора</b>",
        "admin_denied": "⛔ Доступ запрещён.",
        "url_processing": "🔗 Обработка ссылки...",
        "closed": "✅ Поиск закрыт.",
        "cmd_start": "Запустить бота",
        "cmd_language": "Сменить язык",
        "cmd_help": "Помощь",
    },
    "uz": {
        "searching": "🔍 YouTube'da qidirilmoqda...",
        "downloading": "⬇️ Yuklanmoqda...",
        "sending": "📤 Yuborilmoqda...",
        "not_found": "❌ Natija topilmadi.",
        "too_long": "❌ Trek juda uzun (maks. 20 daqiqa).",
        "lyrics_not_found": "❌ Qo'shiq matni topilmadi.",
        "choose_lang": "🌍 Tilni tanlang:",
        "lang_set": "✅ Til O'zbek 🇺🇿 ga o'rnatildi",
        "invalid": "⚠️ Noto'g'ri raqam.",
        "no_search": "⚠️ Avval qo'shiq qidiring.",
        "cached": "⚡ Keshdan yuborilmoqda...",
        "welcome": "👋 <b>Music Stream HQ ga xush kelibsiz!</b> 🎧\n<i>Musiqa qidirish, tanish va yuklash uchun shaxsiy hubingiz.</i>\n\n━━━━━━━━━━━━━━\n🆓 <b>BEPUL REJA (FREE):</b>\n• 🎧 <b>Shazam ornatilgan:</b> TikTok video, Reel yoki ovozli xabar yuboring — qoshiqni bir zumda aniqlang! 🔍\n• 🎵 <b>Kuniga 8 ta yuklab olish</b> (MP3 320kbps yuqori sifat)\n• 🔍 <b>Aqlli qidiruv:</b> Nom, ijrochi yoki havola boyicha qidiring (YouTube, Spotify, SoundCloud)\n• 📜 <b>Qoshiq matnlari</b> — bepul va barchaga ochiq\n• ✈️ <b>Oflayn tinglash:</b> Qoshiqlarni saqlang, parvoz rejimida ham tinglang!\n\n⭐ <b>PREMIUM IMKONIYATLAR (OCHILADIGAN):</b>\n• ♾️ <b>Cheksiz yuklab olishlar</b> — Kunlik limit yoq, kutish yoq\n• 📂 <b>Pleylist &amp; albomlarni yuklab olish</b> — Butun toplamlar 1 bosishda\n• 💎 <b>FLAC / Lossless sifat</b> — Siqilishsiz toza studiya ovozi\n• 🎤 <b>Karaoke / Vocal Remover</b> — Ovozni musiqadan ajratib oling\n• 🌀 <b>8D audio &amp; FX effektlar</b> — 360° fazoviy ovoz, Bass Boost va Nightcore\n• ⚡ <b>Ustuvor navbat</b> — Serverlarda juda tez yuklab olish\n\n━━━━━━━━━━━━━━\n🚀 <i>Boshlashga tayyormisiz? TikTok video, ovozli xabar yuboring yoki qoshiq nomini yozing!</i> 👇",
        "offline_help": '✈️ Treklarni shu chatdan telefoningizga saqlang: yuklab olingach, internetsiz yoki parvoz rejimida ham tinglang!',
        "btn_go_premium": '⭐ Premium olish (Yulduzlar)',
        "btn_offline": '✈️ Oflayn qanday tinglanadi?',
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
        "searching": "🔍 YouTube पर खोज रहे हैं...",
        "downloading": "⬇️ डाउनलोड हो रहा है...",
        "sending": "📤 भेज रहे हैं...",
        "not_found": "❌ कोई परिणाम नहीं मिला।",
        "too_long": "❌ ट्रैक बहुत लंबा है (अधिकतम 20 मिनट)।",
        "lyrics_not_found": "❌ बोल नहीं मिले।",
        "choose_lang": "🌍 अपनी भाषा चुनें:",
        "lang_set": "✅ भाषा हिन्दी 🇮🇳 पर सेट की गई",
        "invalid": "⚠️ अमान्य नंबर।",
        "no_search": "⚠️ पहले एक गाना खोजें।",
        "cached": "⚡ कैश से भेज रहे हैं...",
        "welcome": "👋 <b>Music Stream HQ में आपका स्वागत है!</b> 🎧\n<i>संगीत खोजने, पहचानने और डाउनलोड करने के लिए आपका निजी हब।</i>\n\n━━━━━━━━━━━━━━\n🆓 <b>मुफ़्त प्लान (FREE):</b>\n• 🎧 <b>बिल्ट-इन Shazam:</b> TikTok वीडियो, Reel या वॉइस नोट भेजें और तुरंत गाना पहचानें! 🔍\n• 🎵 <b>प्रतिदिन 8 डाउनलोड</b> (MP3 320kbps उच्च गुणवत्ता)\n• 🔍 <b>स्मार्ट खोज:</b> शीर्षक, कलाकार या लिंक से खोजें (YouTube, Spotify, SoundCloud)\n• 📜 <b>गानों के बोल</b> — सभी के लिए मुफ़्त और अनलॉक\n• ✈️ <b>ऑफ़लाइन सुनना:</b> गाने सेव करें और हवाई जहाज़ मोड में भी सुनें!\n\n⭐ <b>प्रीमियम सुविधाएँ (अनलॉक करने योग्य):</b>\n• ♾️ <b>असीमित डाउनलोड</b> — शून्य दैनिक सीमा, शून्य प्रतीक्षा\n• 📂 <b>प्लेलिस्ट &amp; एल्बम डाउनलोड</b> — पूरा संग्रह 1 क्लिक में\n• 💎 <b>FLAC / Lossless गुणवत्ता</b> — बिना संपीड़न के शुद्ध स्टूडियो ऑडियो\n• 🎤 <b>कराओके / Vocal Remover</b> — बीट से आवाज़ अलग करें\n• 🌀 <b>8D ऑडियो &amp; FX इफ़ेक्ट</b> — 360° स्पेशियल साउंड, Bass Boost &amp; Nightcore\n• ⚡ <b>प्राथमिकता कतार</b> — सर्वर पर अल्ट्रा-फ़ास्ट डाउनलोड\n\n━━━━━━━━━━━━━━\n🚀 <i>शुरू करने के लिए तैयार? TikTok वीडियो, वॉइस नोट भेजें या यहाँ गाने का नाम लिखें!</i> 👇",
        "offline_help": '✈️ इस चैट से गाने अपने फ़ोन में सेव करें: डाउनलोड के बाद बिना इंटरनेट या हवाई जहाज़ मोड में भी सुनें!',
        "btn_go_premium": '⭐ प्रीमियम लें (स्टार्स)',
        "btn_offline": '✈️ ऑफ़लाइन कैसे सुनें?',
        "btn_lang": '🌍 भाषा बदलें',
        "admin_header": "📊 <b>एडमिन पैनल</b>",
        "admin_denied": "⛔ पहुँच अस्वीकृत।",
        "url_processing": "🔗 लिंक प्रोसेस हो रहा है...",
        "closed": "✅ खोज बंद।",
        "cmd_start": "बॉट शुरू करें",
        "cmd_language": "भाषा बदलें",
        "cmd_help": "मदद",
    },
    "zh": {
        "searching": "🔍 正在搜索YouTube...",
        "downloading": "⬇️ 下载中...",
        "sending": "📤 发送中...",
        "not_found": "❌ 未找到结果。",
        "too_long": "❌ 曲目太长（最长20分钟）。",
        "lyrics_not_found": "❌ 未找到歌词。",
        "choose_lang": "🌍 选择您的语言:",
        "lang_set": "✅ 语言已设置为中文 🇨🇳",
        "invalid": "⚠️ 无效号码。",
        "no_search": "⚠️ 请先搜索一首歌。",
        "cached": "⚡ 从缓存发送...",
        "welcome": "👋 <b>欢迎使用 Music Stream HQ！</b> 🎧\n<i>您的个人音乐搜索、识别与下载中心，享受极致音质体验。</i>\n\n━━━━━━━━━━━━━━\n🆓 <b>免费计划 (FREE)：</b>\n• 🎧 <b>内置 Shazam：</b>发送 TikTok 视频、Reel 或语音消息，立即识别歌曲！ 🔍\n• 🎵 <b>每天 8 次下载</b>（MP3 320kbps 高品质）\n• 🔍 <b>智能搜索：</b>按歌名、歌手或链接搜索（YouTube、Spotify、SoundCloud）\n• 📜 <b>歌词</b> — 所有人免费使用\n• ✈️ <b>离线收听：</b>保存歌曲，飞行模式下也能播放！\n\n⭐ <b>高级功能（可解锁）：</b>\n• ♾️ <b>无限下载</b> — 零每日限制，零等待\n• 📂 <b>播放列表 &amp; 专辑下载</b> — 一键下载整个合集\n• 💎 <b>FLAC / 无损音质</b> — 纯粹的录音室音频，无压缩\n• 🎤 <b>卡拉 OK / 人声消除</b> — 将人声与伴奏分离\n• 🌀 <b>8D 音效 &amp; FX 特效</b> — 360° 空间音效、低音增强 &amp; Nightcore\n• ⚡ <b>优先队列</b> — 服务器上超快速下载\n\n━━━━━━━━━━━━━━\n🚀 <i>准备好了吗？发送 TikTok 视频、语音消息，或在此输入歌曲名称！</i> 👇",
        "offline_help": '✈️ 从此聊天将歌曲保存到手机：下载后即使没有网络或在飞行模式下也能收听！',
        "btn_go_premium": '⭐ 升级高级版（星星）',
        "btn_offline": '✈️ 如何离线收听？',
        "btn_lang": '🌍 更换语言',
        "admin_header": "📊 <b>管理员面板</b>",
        "admin_denied": "⛔ 拒绝访问。",
        "url_processing": "🔗 处理链接中...",
        "closed": "✅ 搜索已关闭。",
        "cmd_start": "启动机器人",
        "cmd_language": "更改语言",
        "cmd_help": "帮助",
    },
    "ja": {
        "searching": "🔍 YouTubeを検索中...",
        "downloading": "⬇️ ダウンロード中...",
        "sending": "📤 送信中...",
        "not_found": "❌ 結果が見つかりません。",
        "too_long": "❌ トラックが長すぎます（最大20分）。",
        "lyrics_not_found": "❌ 歌詞が見つかりません。",
        "choose_lang": "🌍 言語を選択してください:",
        "lang_set": "✅ 言語が日本語 🇯🇵 に設定されました",
        "invalid": "⚠️ 無効な番号です。",
        "no_search": "⚠️ まず曲を検索してください。",
        "cached": "⚡ キャッシュから送信中...",
        "welcome": "👋 <b>Music Stream HQ へようこそ！</b> 🎧\n<i>音楽を検索・認識・ダウンロードできる、あなた専用の高音質ハブです。</i>\n\n━━━━━━━━━━━━━━\n🆓 <b>無料プラン (FREE)：</b>\n• 🎧 <b>Shazam 内蔵：</b>TikTok 動画・リール・ボイスメモを送るだけで曲を即座に認識！ 🔍\n• 🎵 <b>1日8回のダウンロード</b>（MP3 320kbps 高音質）\n• 🔍 <b>スマート検索：</b>タイトル・アーティスト・リンクで検索（YouTube・Spotify・SoundCloud）\n• 📜 <b>歌詞</b> — 全員無料で利用可能\n• ✈️ <b>オフライン再生：</b>曲を保存して機内モードでも聴ける！\n\n⭐ <b>プレミアム機能（アンロック可能）：</b>\n• ♾️ <b>無制限ダウンロード</b> — 1日の制限なし、待ち時間なし\n• 📂 <b>プレイリスト &amp; アルバムのダウンロード</b> — コレクションを1クリックで\n• 💎 <b>FLAC / ロスレス品質</b> — 圧縮なしの純粋なスタジオ音質\n• 🎤 <b>カラオケ / ボーカルリムーバー</b> — ビートからボーカルを分離・除去\n• 🌀 <b>8D オーディオ &amp; FX エフェクト</b> — 360° 立体音響、Bass Boost &amp; Nightcore\n• ⚡ <b>優先キュー</b> — サーバーで超高速ダウンロード\n\n━━━━━━━━━━━━━━\n🚀 <i>準備完了？TikTok 動画・ボイスメモを送るか、ここに曲名を入力してください！</i> 👇",
        "offline_help": '✈️ このチャットから曲をスマホに保存：ダウンロード後はネットなしでも機内モードでも聴けます！',
        "btn_go_premium": '⭐ プレミアムへ（スター）',
        "btn_offline": '✈️ オフライン再生の方法',
        "btn_lang": '🌍 言語を変更',
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

URL_PATTERNS = re.compile(
    r"(https?://)?(www\.)?"
    r"(youtube\.com/watch|youtube\.com/shorts/|youtu\.be/|soundcloud\.com/|tiktok\.com/|instagram\.com/(reel|p)/|open\.spotify\.com/)",
    re.IGNORECASE,
)

# --- Shazam / audio-recognition texts (merged into LANGUAGES below) -------
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
           "shz_not_found": "❌ No se reconoció ninguna canción en este audio. ¡Inténtalo con un audio más claro o más largo!",
           "shz_download": "⬇️ Descargar Canción (MP3)"},
    "fr": {"shz_analyzing": "🎧 Analyse de l'audio en cours… quelques secondes ⏳",
           "shz_found": "🎶 <b>Morceau Reconnu !</b>\n\n📌 <b>Titre :</b> {title}\n👤 <b>Artiste :</b> {artist}",
           "shz_not_found": "❌ Aucun morceau reconnu dans cet audio. Réessayez avec un audio plus clair ou plus long !",
           "shz_download": "⬇️ Télécharger le Morceau (MP3)"},
    "de": {"shz_analyzing": "🎧 Audio wird analysiert… einen Moment ⏳",
           "shz_found": "🎶 <b>Song erkannt!</b>\n\n📌 <b>Titel:</b> {title}\n👤 <b>Künstler:</b> {artist}",
           "shz_not_found": "❌ Kein Song in diesem Audio erkannt. Versuche es mit klarerem oder längerem Audio!",
           "shz_download": "⬇️ Song herunterladen (MP3)"},
    "pt": {"shz_analyzing": "🎧 Analisando o áudio… aguarde alguns segundos ⏳",
           "shz_found": "🎶 <b>Música Reconhecida!</b>\n\n📌 <b>Título:</b> {title}\n👤 <b>Artista:</b> {artist}",
           "shz_not_found": "❌ Nenhuma música reconhecida neste áudio. Tente novamente com um áudio mais claro ou mais longo!",
           "shz_download": "⬇️ Baixar Música (MP3)"},
    "ru": {"shz_analyzing": "🎧 Анализ аудио… подождите несколько секунд ⏳",
           "shz_found": "🎶 <b>Трек распознан!</b>\n\n📌 <b>Название:</b> {title}\n👤 <b>Исполнитель:</b> {artist}",
           "shz_not_found": "❌ Трек в этом аудио не распознан. Попробуйте более чёткое или длинное аудио!",
           "shz_download": "⬇️ Скачать трек (MP3)"},
    "uz": {"shz_analyzing": "🎧 Audio tahlil qilinmoqda… bir necha soniya kuting ⏳",
           "shz_found": "🎶 <b>Qo'shiq aniqlandi!</b>\n\n📌 <b>Nomi:</b> {title}\n👤 <b>Ijrochi:</b> {artist}",
           "shz_not_found": "❌ Bu audiodan qo'shiq aniqlanmadi. Aniqroq yoki uzunroq audio bilan qayta urinib ko'ring!",
           "shz_download": "⬇️ Qo'shiqni yuklab olish (MP3)"},
    "hi": {"shz_analyzing": "🎧 ऑडियो का विश्लेषण हो रहा है… कुछ सेकंड रुकें ⏳",
           "shz_found": "🎶 <b>गाना पहचाना गया!</b>\n\n📌 <b>शीर्षक:</b> {title}\n👤 <b>कलाकार:</b> {artist}",
           "shz_not_found": "❌ इस ऑडियो में कोई गाना नहीं पहचाना गया। साफ़ या लंबे ऑडियो के साथ फिर से कोशिश करें!",
           "shz_download": "⬇️ गाना डाउनलोड करें (MP3)"},
    "zh": {"shz_analyzing": "🎧 正在分析音频…请稍等几秒 ⏳",
           "shz_found": "🎶 <b>歌曲已识别！</b>\n\n📌 <b>标题：</b>{title}\n👤 <b>歌手：</b>{artist}",
           "shz_not_found": "❌ 未能从该音频中识别出歌曲。请用更清晰或更长的音频重试！",
           "shz_download": "⬇️ 下载歌曲 (MP3)"},
    "ja": {"shz_analyzing": "🎧 オーディオを解析中… 数秒お待ちください ⏳",
           "shz_found": "🎶 <b>曲を認識しました！</b>\n\n📌 <b>タイトル:</b> {title}\n👤 <b>アーティスト:</b> {artist}",
           "shz_not_found": "❌ この音声から曲を認識できませんでした。よりクリアで長い音声でもう一度お試しください！",
           "shz_download": "⬇️ 曲をダウンロード (MP3)"},
}
for _code, _d in SHAZAM_TEXTS.items():
    LANGUAGES[_code].update(_d)

# ---------------------------------------------------------------------------
# Database (WAL mode for speed)
# ---------------------------------------------------------------------------


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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
    """)
    # Premium / daily-limit columns (idempotent migration)
    for ddl in (
        "ALTER TABLE users ADD COLUMN is_premium INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN premium_until TEXT",
        "ALTER TABLE users ADD COLUMN daily_downloads_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN last_download_date TEXT",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
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
    cached = lang_cache.get(user_id)  # atomic read — safe vs. concurrent clear()
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


# ---------------------------------------------------------------------------
# Premium / daily-limit helpers
# ---------------------------------------------------------------------------

FREE_DAILY_LIMIT = 8


def is_premium(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True  # the owner always has full Premium access, free forever
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT is_premium, premium_until FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.close()
        if not row or not row["is_premium"]:
            return False
        until = row["premium_until"]
        if until and datetime.utcnow() > datetime.fromisoformat(until):
            conn = get_db()
            conn.execute("UPDATE users SET is_premium = 0 WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()
            return False
        return True
    except Exception as exc:
        log.error("is_premium error: %s", exc)
        return False


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
    """Extend (or start) premium; returns new expiry."""
    conn = get_db()
    row = conn.execute(
        "SELECT premium_until FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    base = datetime.utcnow()
    if row and row["premium_until"]:
        try:
            cur = datetime.fromisoformat(row["premium_until"])
            if cur > base:
                base = cur  # extend an active subscription
        except ValueError:
            pass
    until = base + timedelta(days=days)
    conn.execute(
        """INSERT INTO users (user_id, is_premium, premium_until) VALUES (?, 1, ?)
           ON CONFLICT(user_id) DO UPDATE SET is_premium = 1, premium_until = excluded.premium_until""",
        (user_id, until.isoformat()),
    )
    conn.commit()
    conn.close()
    return until


def check_daily_limit(user_id: int) -> bool:
    """True if the user may download now (atomic check+increment for free users)."""
    if is_premium(user_id):
        return True
    today = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )
        # Single conditional UPDATE: one accepted download == one counted unit.
        cur = conn.execute(
            """UPDATE users SET
                   daily_downloads_count = CASE
                       WHEN last_download_date = ? THEN daily_downloads_count + 1
                       ELSE 1 END,
                   last_download_date = ?
               WHERE user_id = ?
                 AND (last_download_date IS NOT ?
                      OR daily_downloads_count < ?)""",
            (today, today, user_id, today, FREE_DAILY_LIMIT),
        )
        allowed = cur.rowcount == 1
        conn.commit()
        conn.close()
        return allowed
    except Exception as exc:
        log.error("check_daily_limit error: %s", exc)
        return True  # never block on a DB hiccup


def get_admin_stats() -> dict:
    conn = get_db()
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    monthly_active = conn.execute(
        "SELECT COUNT(*) FROM users WHERE last_active >= ?",
        (datetime.utcnow() - timedelta(days=30),),
    ).fetchone()[0]
    total_downloads = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
    conn.close()
    return {"total_users": total_users, "monthly_active": monthly_active, "total_downloads": total_downloads}


def t(user_id: int, key: str) -> str:
    strings = LANGUAGES.get(get_user_lang(user_id), LANGUAGES["en"])
    return strings.get(key, LANGUAGES["en"].get(key, key))


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------

MAX_TRACK_SECONDS = 1200  # 20 min — 320kbps beyond this exceeds Telegram's 50MB limit

# Punchy "party" master: tight low-end thump, crisp presence, bright air,
# glue compression and loud streaming-level normalization.
EQ_FILTER = (
    "highpass=f=25,"
    "bass=g=6:f=90:w=0.6,"
    "equalizer=f=250:width_type=q:width=1.2:g=-1.5,"   # cut mud
    "equalizer=f=3200:width_type=q:width=1.0:g=2.5,"   # vocal presence
    "treble=g=3:f=7800,"
    "equalizer=f=12500:width_type=q:width=0.8:g=2,"    # air / sparkle
    "acompressor=threshold=-16dB:ratio=2.5:attack=12:release=180:makeup=2,"
    "loudnorm=I=-11:LRA=9:TP=-1.0"
)


def search_youtube(query: str, max_results: int = SEARCH_POOL) -> list[dict]:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    results = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
        for entry in info.get("entries", []):
            if not entry or not entry.get("id"):
                continue
            results.append({
                "id": entry["id"],
                "url": f"https://www.youtube.com/watch?v={entry['id']}",
                "title": entry.get("title", "Unknown"),
                "uploader": entry.get("uploader") or entry.get("channel") or "Unknown",
                "duration": entry.get("duration") or 0,
                "thumbnail": f"https://i.ytimg.com/vi/{entry['id']}/mqdefault.jpg",
            })
    return results


def get_audio_stream_url(video_url: str) -> dict | None:
    opts = {
        "quiet": True, "no_warnings": True,
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "skip_download": True, "socket_timeout": 8,
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
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail"},
        ],
        "postprocessor_args": {"ffmpegextractaudio": ["-af", EQ_FILTER]},
        "writethumbnail": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        # Hard safety net: never download tracks over the length cap
        # (320kbps beyond ~20 min exceeds Telegram's 50MB bot upload limit)
        "match_filter": yt_dlp.utils.match_filter_func(f"duration<={MAX_TRACK_SECONDS}"),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
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
    """Trim noisy YouTube titles to a clean 'Artist – Title' like VK Music Bot."""
    # keep only the part before the first pipe segment spam
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
# Results keyboard & message (photo-1 style)
# ---------------------------------------------------------------------------


def build_results_keyboard() -> types.ReplyKeyboardMarkup:
    """Numbers 1-5 / 6-10 in two rows + navigation row ⬅️ ❌ ➡️."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.row(*[types.KeyboardButton(str(i)) for i in range(1, 6)])
    markup.row(*[types.KeyboardButton(str(i)) for i in range(6, 11)])
    markup.row(types.KeyboardButton(BTN_PREV), types.KeyboardButton(BTN_CLOSE), types.KeyboardButton(BTN_NEXT))
    return markup


def build_results_text(query: str, results: list[dict], offset: int) -> str:
    lines = [f"🎙 <b>{esc(query)}</b>", ""]
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
    """For inline-mode results: deep link opens the bot chat and delivers lyrics
    (callback buttons can't send messages in chats where the bot isn't present)."""
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
    """Aggressively clean YouTube junk out of title/artist for lyrics lookups."""
    title = _INVISIBLE_RE.sub("", title or "")
    performer = re.sub(r"\s*-\s*Topic$", "", _INVISIBLE_RE.sub("", performer or "")).strip()
    title = _NOISE_RE.sub("", title)
    title = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*$", "", title).strip()  # trailing (...) leftovers
    # split "Artist - Song" if embedded in the title
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


_GENIUS_MIRRORS = ["https://dumb.ducks.party", "https://dm.vern.cc"]


def _slugify_genius(artist: str, track: str) -> str:
    s = f"{artist} {track}".strip()
    s = re.sub(r"[’']", "", s)
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return f"{s.capitalize()}-lyrics" if s else ""


def _find_genius_path(track: str, artist: str) -> str | None:
    """Find the Genius song path via DuckDuckGo (Google-style web search)."""
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"{artist} {track} lyrics site:genius.com"},
            timeout=8, headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.ok:
            for link in re.findall(r'genius\.com(/[A-Za-z0-9%\-]+-lyrics)', resp.text):
                return requests.utils.unquote(link)
    except Exception:
        pass
    return None


def _extract_div(html_text: str, div_id: str) -> str | None:
    """Return inner HTML of <div id=...> handling nested divs."""
    m = re.search(rf'<div[^>]*id="{div_id}"[^>]*>', html_text)
    if not m:
        return None
    depth, i = 1, m.end()
    for tag in re.finditer(r"<div\b|</div>", html_text[m.end():]):
        depth += 1 if tag.group() != "</div>" else -1
        if depth == 0:
            i = m.end() + tag.start()
            break
    return html_text[m.end():i]


def _mirror_search(track: str, artist: str) -> str | None:
    """Search directly on the Genius mirror — most reliable path finder."""
    for mirror in _GENIUS_MIRRORS:
        try:
            resp = requests.get(f"{mirror}/search", params={"q": f"{artist} {track}".strip()},
                                timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if resp.ok:
                hits = re.findall(r'href="(/[A-Za-z0-9%\-]+-lyrics)"', resp.text)
                if hits:
                    return hits[0]
        except Exception:
            continue
    return None


def _lyrics_genius(track: str, artist: str) -> str | None:
    """Genius lyrics via mirror search / web search / slug guess."""
    paths = []
    for candidate in (
        _mirror_search(track, artist),
        _find_genius_path(track, artist),
        f"/{_slugify_genius(artist, track)}" if _slugify_genius(artist, track) else None,
    ):
        if candidate and candidate not in paths:
            paths.append(candidate)
    for path in paths:
        for mirror in _GENIUS_MIRRORS:
            try:
                page = requests.get(f"{mirror}{path}", timeout=8,
                                    headers={"User-Agent": "Mozilla/5.0"})
                if not page.ok:
                    continue
                page.encoding = "utf-8"  # mirrors omit charset → avoid mojibake on accents
                inner = _extract_div(page.text, "lyrics")
                if not inner:
                    continue
                raw = re.sub(r"<br\s*/?>", "\n", inner)
                raw = re.sub(r"</p>", "\n\n", raw)
                raw = re.sub(r"<[^>]+>", "", raw)
                text = html.unescape(raw).strip()
                if len(text) > 40:
                    return text
            except Exception:
                continue
    return None


def fetch_lyrics(title: str, performer: str) -> str | None:
    """Multi-source lyrics: lrclib → Genius → lyrics.ovh → AI (if configured)."""
    track, artist = normalize_track(title, performer)
    for source in (_lyrics_lrclib, _lyrics_genius, _lyrics_ovh):
        text = source(track, artist)
        if text:
            return text
    # retry with swapped artist/track (YouTube often reverses them)
    if artist:
        for source in (_lyrics_lrclib, _lyrics_genius):
            text = source(artist, track)
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
# Premium UI, Telegram Stars payments & audio effects
# ---------------------------------------------------------------------------

PREMIUM_TEXTS = {
    "it": {
        "limit_reached": "⚠️ <b>Download momentaneamente finiti per oggi!</b>\n\nHai raggiunto il tuo limite giornaliero di <b>8 canzoni gratuite</b>.\n\nSe vuoi continuare a scaricare senza attendere domani e sbloccare tutte le funzioni avanzate, abbonati subito a Premium con le Stelle di Telegram! 🚀",
        "playlist_premium": "📂 <b>Download Playlist Riservato ai Membri Premium!</b>\n\nPer scaricare interi album o playlist in 1 solo click, passa a Premium con le Stelle di Telegram!",
        "locked_alert": "🔒 Funzione Riservata ai Membri Premium!",
        "subscribe_text": "⭐ <b>Music Stream HQ Premium</b>\n\n✅ Download illimitati (niente limite di 8/giorno)\n✅ Download playlist e album interi\n✅ 🎤 Vocal Remover (karaoke)\n✅ 🌀 Audio 8D\n✅ 🎛️ Audio FX (Bass Boost, Nightcore, Slowed)\n\nScegli il tuo piano e paga con le Stelle di Telegram:",
        "status_free": "👤 <b>Account: FREE</b>\n\n⬇️ Download oggi: {used}/8\n\nPassa a Premium con /subscribe per download illimitati e funzioni VIP! ⭐",
        "status_premium": "💎 <b>Account: PREMIUM</b>\n\n⏳ Tempo rimanente: <b>{days}g {hours}h {minutes}m</b>\n\nGrazie del supporto! 🚀",
        "payment_ok": "🎉 <b>Pagamento ricevuto — Benvenuto in Premium!</b>\n\n💎 Valido fino al: <b>{until}</b> (UTC)\n\nTutte le funzioni VIP sono attive da ORA! 🚀",
        "granted": "🎁 <b>Hai ricevuto {days} giorni di Premium!</b>\n\n💎 Valido fino al: <b>{until}</b> (UTC). Goditi le funzioni VIP! 🚀",
        "processing_fx": "🎛 Elaborazione audio in corso... attendi qualche istante ⏳",
        "fx_menu": "🎛️ <b>Audio FX</b> — scegli un effetto:",
        "fx_error": "❌ Elaborazione non riuscita. Riprova più tardi.",
        "vip_alert": "💎 Sei un membro Premium! Usa /status per i dettagli.",
        "btn_unlock": "⭐ Sblocca Premium",
        "btn_vip": "💎 Status VIP",
    },
    "en": {
        "limit_reached": "⚠️ <b>No more downloads for today!</b>\n\nYou reached your daily limit of <b>8 free songs</b>.\n\nTo keep downloading without waiting for tomorrow and unlock all advanced features, subscribe to Premium with Telegram Stars! 🚀",
        "playlist_premium": "📂 <b>Playlist downloads are for Premium members!</b>\n\nTo download entire albums or playlists in 1 click, go Premium with Telegram Stars!",
        "locked_alert": "🔒 Premium members only!",
        "subscribe_text": "⭐ <b>Music Stream HQ Premium</b>\n\n✅ Unlimited downloads (no 8/day limit)\n✅ Full playlist & album downloads\n✅ 🎤 Vocal Remover (karaoke)\n✅ 🌀 8D Audio\n✅ 🎛️ Audio FX (Bass Boost, Nightcore, Slowed)\n\nPick your plan and pay with Telegram Stars:",
        "status_free": "👤 <b>Account: FREE</b>\n\n⬇️ Downloads today: {used}/8\n\nGo Premium with /subscribe for unlimited downloads and VIP features! ⭐",
        "status_premium": "💎 <b>Account: PREMIUM</b>\n\n⏳ Time left: <b>{days}d {hours}h {minutes}m</b>\n\nThanks for your support! 🚀",
        "payment_ok": "🎉 <b>Payment received — Welcome to Premium!</b>\n\n💎 Valid until: <b>{until}</b> (UTC)\n\nAll VIP features are active NOW! 🚀",
        "granted": "🎁 <b>You received {days} days of Premium!</b>\n\n💎 Valid until: <b>{until}</b> (UTC). Enjoy the VIP features! 🚀",
        "processing_fx": "🎛 Processing audio... hold on ⏳",
        "fx_menu": "🎛️ <b>Audio FX</b> — pick an effect:",
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


PLANS = [  # (label, days, stars)
    ("🚀 1 Day (Trial) — 20 ⭐️", 1, 20),
    ("⭐ 1 Month — 100 ⭐️", 30, 100),
    ("💫 3 Months — 250 ⭐️", 90, 250),
    ("👑 6 Months — 500 ⭐️", 180, 500),
]

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

_PLAYLIST_RE = re.compile(r"[?&]list=|/playlist|/album/|/sets/[^/]+/?$|/sets/", re.IGNORECASE)

EFFECTS = {
    "vocal": ("🎤 Vocal Remover", "pan=stereo|c0=c0-c1|c1=c1-c0,loudnorm=I=-14:TP=-1.5"),
    "8d": ("🌀 Audio 8D", "apulsator=hz=0.09,stereowiden=delay=18:feedback=0.4:crossfeed=0.35"),
    "bass": ("🔊 Bass Boost", "bass=g=12:f=80:w=0.5,acompressor=threshold=-12dB:ratio=3:makeup=3,loudnorm=I=-11:TP=-1.0"),
    "night": ("⚡ Nightcore", "asetrate=44100*1.25,aresample=44100"),
    "slow": ("🌙 Slowed + Reverb", "asetrate=44100*0.85,aresample=44100,aecho=0.8:0.85:60|90:0.25|0.2"),
}


def track_keyboard(video_id: str, user_id: int) -> types.InlineKeyboardMarkup | None:
    """Dynamic keyboard under MP3s in PRIVATE chats (premium vs free)."""
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
        types.InlineKeyboardButton(f"🎛️ Audio FX{lock}", callback_data=f"fxm:{video_id}"),
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
            provider_token="",  # Telegram Stars: empty provider token
            currency="XTR",
            prices=[types.LabeledPrice(label=f"Premium {days}d", amount=stars)],
        )
    except Exception as exc:
        log.error("send_invoice error: %s", exc)


def _validate_plan(payload: str, currency: str, amount: int) -> int | None:
    """Return plan days only if payload+currency+amount match a known plan."""
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
            log.warning("Rejected pre_checkout: payload=%r %s %s",
                        query.invoice_payload, query.currency, query.total_amount)
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
            log.error("Unexpected successful_payment: payload=%r %s %s (user %s)",
                      sp.invoice_payload, sp.currency, sp.total_amount, uid)
            bot.send_message(message.chat.id, "⚠️ Payment received but plan unrecognized — contact support: /admin")
            return
        until = grant_premium(uid, days)
        bot.send_message(
            message.chat.id,
            pxt(uid, "payment_ok").format(until=until.strftime("%d/%m/%Y %H:%M")),
        )
    except Exception as exc:
        log.error("successful_payment error: %s", exc)


@bot.message_handler(commands=["status"])
def cmd_status(message: types.Message):
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
                "SELECT daily_downloads_count, last_download_date FROM users WHERE user_id = ?",
                (uid,),
            ).fetchone()
            conn.close()
            used = row["daily_downloads_count"] if row and row["last_download_date"] == today else 0
            bot.send_message(message.chat.id, pxt(uid, "status_free").format(used=used))
    except Exception as exc:
        log.error("/status error: %s", exc)


def _resolve_user(token: str) -> int | None:
    """Resolve '@username', 'username' or numeric id to a user_id from the DB."""
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
        return  # solo l'admin — per tutti gli altri il comando non esiste
    try:
        parts = message.text.split()
        # accetta: /regala @utente 30  |  /regala 123456 5 giorni
        if len(parts) < 3:
            raise ValueError
        target_token = parts[1]
        days_token = next((p for p in parts[2:] if p.isdigit()), None)
        if days_token is None:
            raise ValueError
        days = int(days_token)
        target = _resolve_user(target_token)
        if target is None:
            bot.send_message(
                message.chat.id,
                f"❌ Utente {esc(target_token)} non trovato. Deve aver avviato il bot almeno una volta "
                "(oppure usa il suo ID numerico).",
            )
            return
        until = grant_premium(target, days)
        bot.send_message(message.chat.id,
                         f"🎁 Premium regalato a <code>{target}</code> ({esc(target_token)}) — {days} giorni "
                         f"(fino al {until.strftime('%d/%m/%Y %H:%M')} UTC) ✅")
        try:
            bot.send_message(target, pxt(target, "granted").format(
                days=days, until=until.strftime("%d/%m/%Y %H:%M")))
        except Exception:
            pass  # user may have never started the bot
    except ValueError:
        bot.send_message(message.chat.id,
                         "Uso: /regala @username 30  oppure  /regala <user_id> <giorni>")
    except Exception as exc:
        log.error("/regala error: %s", exc)


def _apply_effect_and_send(chat_id: int, user_id: int, video_id: str, effect: str) -> None:
    """Premium: re-render the track with an FFmpeg effect and send it."""
    label, afilter = EFFECTS[effect]
    status = None
    mp3_path = None
    try:
        status = bot.send_message(chat_id, pxt(user_id, "processing_fx"))
        mp3_path, meta = download_audio(f"https://www.youtube.com/watch?v={video_id}")
        if not mp3_path:
            bot.send_message(chat_id, pxt(user_id, "fx_error"))
            return
        out_path = os.path.join(os.path.dirname(mp3_path), f"fx_{effect}.mp3")
        proc = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", mp3_path, "-af", afilter, "-b:a", "320k", out_path],
            capture_output=True, timeout=300,
        )
        if proc.returncode != 0 or not os.path.exists(out_path):
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
        if mp3_path:
            shutil.rmtree(os.path.dirname(mp3_path), ignore_errors=True)
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
    if not is_premium(uid):
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
        if call.data.startswith("fxm:"):  # FX submenu
            video_id = call.data.split(":", 1)[1]
            if not _VIDEO_ID_RE.match(video_id):
                return
            kb = types.InlineKeyboardMarkup(row_width=1)
            for key in ("bass", "night", "slow"):
                kb.add(types.InlineKeyboardButton(EFFECTS[key][0], callback_data=f"fxa:{key}:{video_id}"))
            bot.send_message(chat_id, pxt(uid, "fx_menu"), reply_markup=kb)
            return
        parts = call.data.split(":", 2)  # eff:<effect>:<vid> | fxa:<effect>:<vid>
        if len(parts) != 3:
            return
        _, effect, video_id = parts
        if effect not in EFFECTS or not _VIDEO_ID_RE.match(video_id):
            return
        threading.Thread(target=_apply_effect_and_send, args=(chat_id, uid, video_id, effect), daemon=True).start()
    except Exception as exc:
        log.error("cb_effects error: %s", exc)


# ---------------------------------------------------------------------------
# Shazam — audio recognition (free for everyone)
# ---------------------------------------------------------------------------

_SHORTVID_RE = re.compile(r"tiktok\.com/|instagram\.com/(?:reel|p)/|youtube\.com/shorts/", re.IGNORECASE)
_SHZ_MAX_FILE = 20 * 1024 * 1024  # Bot API download cap


def _shazam_recognize(path: str) -> dict | None:
    """Run shazamio (async) inside a worker thread with its own event loop."""
    import asyncio
    from shazamio import Shazam
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(Shazam().recognize(path))
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _clip_for_shazam(src: str, dst: str) -> bool:
    """Extract the first ~12 seconds of audio as a small MP3 for recognition."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-t", "12", "-vn",
             "-acodec", "libmp3lame", "-b:a", "128k", "-ac", "1", "-ar", "44100", dst],
            check=True, capture_output=True, timeout=90,
        )
        return os.path.exists(dst) and os.path.getsize(dst) > 0
    except Exception as exc:
        log.error("shazam clip error: %s", exc)
        return False


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
    """Clip → Shazam → reply with track card (or polite failure / fallback download)."""
    status_id = None
    try:
        try:
            status_id = bot.send_message(chat_id, t(uid, "shz_analyzing")).message_id
        except Exception:
            pass
        clip = os.path.join(tmpdir, "clip.mp3")
        track = None
        if _clip_for_shazam(media_path, clip):
            try:
                out = _shazam_recognize(clip)
                track = (out or {}).get("track")
            except Exception as exc:
                log.error("shazam recognize error: %s", exc)
        if status_id:
            try:
                bot.delete_message(chat_id, status_id)
            except Exception:
                pass
        if not track:
            if fallback_url:
                # link flow: not recognized → just download the link's audio as before
                start_download(chat_id, fallback_url, uid)
            else:
                bot.send_message(chat_id, t(uid, "shz_not_found"))
            return
        title = track.get("title") or "?"
        artist = track.get("subtitle") or "?"
        cover = (track.get("images") or {}).get("coverart")
        caption = t(uid, "shz_found").format(title=esc(title), artist=esc(artist))
        # find the track on YouTube so the download/effects/lyrics buttons work
        video_id = ""
        try:
            results = search_youtube(f"{artist} {title}", max_results=1)
            if results:
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
    if not flood_ok(uid):
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
    """Short-video link (TikTok / Reels / Shorts): recognize the song first."""
    tmpdir = tempfile.mkdtemp(prefix="shazam_")
    src = os.path.join(tmpdir, "linkaudio.m4a")
    try:
        opts = {
            "format": "bestaudio/best", "outtmpl": src, "quiet": True,
            "no_warnings": True, "noplaylist": True, "socket_timeout": 30,
            "match_filter": yt_dlp.utils.match_filter_func("duration < 600"),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        log.error("shazam link download error: %s", exc)
        shutil.rmtree(tmpdir, ignore_errors=True)
        # couldn't fetch for recognition → fall back to the normal download flow
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
        # start_download enforces the 8/day free quota and shows /subscribe when hit
        start_download(chat_id, f"https://www.youtube.com/watch?v={video_id}",
                       call.from_user.id, video_id)
    except Exception as exc:
        log.error("cb_shazam_download error: %s", exc)


# ---------------------------------------------------------------------------
# Audio delivery
# ---------------------------------------------------------------------------


def start_download(chat_id: int, video_url: str, user_id: int, video_id: str = "") -> None:
    """Queue a download on the bounded pool; max 1 concurrent download per user."""
    # Admission first (one download at a time per user), THEN consume quota —
    # so simultaneous taps can never burn two quota units for one job.
    with _active_lock:
        if user_id in active_downloads:
            return  # a download is already running for this user — ignore extra taps
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
            send_audio_track(chat_id, video_url, user_id, video_id)
        except Exception as exc:
            log.error("Download job crashed: %s", exc)
        finally:
            with _active_lock:
                active_downloads.discard(user_id)

    download_pool.submit(job)


def send_audio_track(chat_id: int, video_url: str, user_id: int, video_id: str = "") -> None:
    is_private = chat_id > 0  # groups/channels have negative ids
    cached = get_cached_file_id(video_id) if video_id else None
    if cached:
        bot.send_audio(
            chat_id, audio=cached["file_id"], title=cached["title"],
            performer=cached["performer"], duration=cached["duration"],
            reply_markup=track_keyboard(video_id, user_id) if is_private else None,
        )
        return

    status_msg = bot.send_message(chat_id, t(user_id, "downloading"))
    mp3_path, meta = download_audio(video_url)
    if not mp3_path:
        err_key = "too_long" if meta.get("error") == "too_long" else "not_found"
        bot.edit_message_text(t(user_id, err_key), chat_id, status_msg.message_id)
        return

    try:
        bot.edit_message_text(t(user_id, "sending"), chat_id, status_msg.message_id)
    except Exception:
        pass

    try:
        thumb_bytes = None
        if meta.get("thumbnail"):
            try:
                resp = requests.get(meta["thumbnail"], timeout=8)
                if resp.ok:
                    thumb_bytes = resp.content
            except Exception:
                pass

        with open(mp3_path, "rb") as audio_file:
            sent = bot.send_audio(
                chat_id, audio=audio_file, title=meta["title"],
                performer=meta["performer"], duration=meta["duration"],
                thumbnail=thumb_bytes,
                reply_markup=track_keyboard(meta.get("video_id", ""), user_id) if is_private else None,
            )
        if meta.get("video_id"):
            save_download(meta["video_id"], sent.audio.file_id, meta["title"], meta["performer"], meta["duration"])
    except Exception as exc:
        log.error("Send audio error: %s", exc)
        bot.send_message(chat_id, t(user_id, "not_found"))
    finally:
        shutil.rmtree(os.path.dirname(mp3_path), ignore_errors=True)
        try:
            bot.delete_message(chat_id, status_msg.message_id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message):
    uid = message.from_user.id
    upsert_user(uid, message.from_user.username)
    # Deep link from an inline "Lyrics" button: /start lyr_<video_id>
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("lyr_"):
        video_id = parts[1][4:]

        def worker():
            track = get_cached_file_id(video_id)
            text = fetch_lyrics(track["title"], track["performer"]) if track else None
            if not text:
                # not in cache (fresh inline track) → try resolving metadata from YouTube
                if not track:
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
    bot.send_message(message.chat.id, t(uid, "welcome"), reply_markup=start_keyboard(uid))


def start_keyboard(uid: int) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(t(uid, "btn_go_premium"), callback_data="sub"))
    markup.add(
        types.InlineKeyboardButton(t(uid, "btn_offline"), callback_data="offhelp"),
        types.InlineKeyboardButton(t(uid, "btn_lang"), callback_data="langmenu"),
    )
    return markup


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
# Navigation buttons (⬅️ ❌ ➡️)
# ---------------------------------------------------------------------------


@bot.message_handler(func=lambda m: m.text in (BTN_PREV, BTN_NEXT, BTN_CLOSE))
def handle_navigation(message: types.Message):
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
    if not flood_ok(uid):
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
    start_download(message.chat.id, track["url"], uid, track["id"])


# ---------------------------------------------------------------------------
# URL handler
# ---------------------------------------------------------------------------


MAX_PLAYLIST_TRACKS = 10


def _download_playlist(chat_id: int, user_id: int, url: str) -> None:
    """Premium: fetch playlist entries and send tracks one by one (bounded)."""
    try:
        opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
                "playlistend": MAX_PLAYLIST_TRACKS, "socket_timeout": 20}
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


@bot.message_handler(func=lambda m: m.text and URL_PATTERNS.search(m.text))
def handle_url(message: types.Message):
    uid = message.from_user.id
    if not flood_ok(uid):
        return
    upsert_user(uid, message.from_user.username)
    url = message.text.strip()
    # Playlists & albums are Premium-only
    if _PLAYLIST_RE.search(url):
        if not is_premium(uid):
            bot.send_message(message.chat.id, pxt(uid, "playlist_premium"))
            send_subscribe_panel(message.chat.id, uid)
            return
        bot.send_message(message.chat.id, t(uid, "url_processing"))
        threading.Thread(target=_download_playlist, args=(message.chat.id, uid, url), daemon=True).start()
        return
    # Short videos (TikTok / Reels / Shorts): Shazam-recognize the song first
    if _SHORTVID_RE.search(url):
        threading.Thread(target=_recognize_from_link, args=(message.chat.id, uid, url), daemon=True).start()
        return
    bot.send_message(message.chat.id, t(uid, "url_processing"))
    yt_match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    video_id = yt_match.group(1) if yt_match else ""
    start_download(message.chat.id, url, uid, video_id)


# ---------------------------------------------------------------------------
# Mode A — text search
# ---------------------------------------------------------------------------


@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
def handle_search(message: types.Message):
    uid = message.from_user.id
    if not flood_ok(uid):
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
# Mode B — inline queries (@MusicStreeambot <query>) with cover art
# ---------------------------------------------------------------------------


@bot.inline_handler(func=lambda query: True)
def handle_inline(inline_query: types.InlineQuery):
    query_text = inline_query.query.strip()
    if not query_text:
        # Instant suggestions: show recently cached tracks as soon as the
        # user tags the bot, before they type anything.
        recent = get_recent_cached_tracks(limit=10)
        results = [
            types.InlineQueryResultCachedAudio(
                id=f"c_{tr['video_id']}", audio_file_id=tr["file_id"],
                reply_markup=lyrics_keyboard_inline(tr["video_id"]),
            )
            for tr in recent
        ]
        bot.answer_inline_query(inline_query.id, results, cache_time=30)
        return

    inline_results: list = []
    seen_ids: set[str] = set()

    # 1) Cached tracks first — instant, with embedded cover art (like photo 3)
    for track in search_cached_tracks(query_text, limit=10):
        inline_results.append(
            types.InlineQueryResultCachedAudio(
                id=f"c_{track['video_id']}",
                audio_file_id=track["file_id"],
                reply_markup=lyrics_keyboard_inline(track["video_id"]),
            )
        )
        seen_ids.add(track["video_id"])

    # 2) Fresh YouTube results — extract stream URLs in PARALLEL for speed
    if len(inline_results) < 10:
        try:
            candidates = [
                r for r in search_youtube(query_text, max_results=8)
                if r["id"] not in seen_ids
            ][: 10 - len(inline_results)]
        except Exception as exc:
            log.error("Inline search error: %s", exc)
            candidates = []

        if candidates:
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = {pool.submit(get_audio_stream_url, c["url"]): c for c in candidates}
                for fut in as_completed(futures, timeout=9):
                    c = futures[fut]
                    try:
                        stream = fut.result()
                    except Exception:
                        continue
                    if not stream or not stream.get("audio_url"):
                        continue
                    inline_results.append(
                        types.InlineQueryResultAudio(
                            id=c["id"],
                            audio_url=stream["audio_url"],
                            title=c["title"],
                            performer=c["uploader"],
                            audio_duration=c["duration"],
                            reply_markup=lyrics_keyboard_inline(c["id"]),
                        )
                    )

    try:
        bot.answer_inline_query(inline_query.id, inline_results[:10], cache_time=60, is_personal=False)
    except Exception as exc:
        log.error("answer_inline_query error: %s", exc)


# ---------------------------------------------------------------------------
# Command panel (menu button in Telegram UI)
# ---------------------------------------------------------------------------


def setup_command_panel() -> None:
    # Default (English) commands
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

    # Localized command panels for each supported language
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

    # ── Kill any active webhook / other polling instance (fixes 409 Conflict) ──
    # Retry a few times in case a Railway deploy is still shutting down.
    for _attempt in range(6):
        try:
            bot.delete_webhook(drop_pending_updates=True)
            log.info("Webhook cleared (attempt %d)", _attempt + 1)
            break
        except Exception as _e:
            log.warning("delete_webhook failed (%d/6): %s — retrying in 5s", _attempt + 1, _e)
            time.sleep(5)

    threading.Thread(target=_auto_cleaner, daemon=True, name="auto-cleaner").start()
    log.info("Bot polling started...")
    while True:
        try:
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=30,
                logger_level=logging.WARNING,
                allowed_updates=["message", "callback_query", "inline_query", "chosen_inline_result"],
                # Discard stale updates accumulated while the other instance ran
                skip_pending=True,
            )
        except Exception as exc:
            log.error("Polling crashed, restarting in 10s: %s", exc)
            time.sleep(10)
            # Clear webhook again before each retry to evict any zombie instance
            try:
                bot.delete_webhook(drop_pending_updates=True)
            except Exception:
                pass
