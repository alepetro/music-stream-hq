# Music Stream HQ Bot 🎧

Studio-quality multi-language Telegram music bot. Searches, downloads and delivers MP3 320kbps tracks with EQ mastering, cover art, lyrics, Shazam recognition, and Premium audio effects.

## Features

- 🎵 Unlimited free MP3 downloads (320kbps + EQ mastering)
- 🔍 Search by title, artist or direct link (YouTube, Spotify, SoundCloud)
- 🎧 Built-in Shazam — send a TikTok/Reel/voice note to identify a song
- 📜 Lyrics (lrclib.net + lyrics.ovh)
- 🌍 11 languages
- ⭐ Premium via Telegram Stars (playlist/album download, vocal remover, 8D audio, FX effects)

## Deploy on Railway

1. Fork or clone this repo
2. Create a new Railway project → **Deploy from GitHub repo**
3. Add a **Postgres** plugin (for premium persistence across deploys)
4. Set environment variables in Railway **Variables** tab:
   - `TELEGRAM_TOKEN` — your bot token from @BotFather
   - `DATABASE_URL` — auto-filled by the Postgres plugin
5. Deploy — Railway uses the `Dockerfile` automatically

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_TOKEN` | ✅ | Bot token from @BotFather |
| `DATABASE_URL` | Optional | Postgres URL for premium persistence |
| `PUBLIC_BASE_URL` | Optional | Your Railway public domain |

## Local Development

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN=your_token
python bot.py
```

Requires `ffmpeg` installed on the system.

## Fix Notes (July 2025)

- **YouTube bot detection bypass**: switched to `ios` player client — fixes "music not found" errors caused by Google's updated bot detection
- **Support button**: restored in `/start` keyboard
- **Promo video**: gracefully skipped if `assets/promo_start.mp4` is missing (upload it to re-enable)
