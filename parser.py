import os
import re
import json
import threading
import requests
import sys
import datetime
import argparse
import logging
import time
import html
from yt_dlp import YoutubeDL
from concurrent.futures import ThreadPoolExecutor

# ==================== ИСХОДНЫЕ НАСТРОЙКИ ====================
API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
CHANNEL_STREAMS_URL = 'https://www.youtube.com/@kvashenaya/streams'
DB_FILE = "parsed_streams_db.json"
OUTPUT_HTML = "index.html"
TRACKLISTS_HTML = "tracklists.html"
PLAYER_HTML = "player.html"
NEW_STREAMS_TO_CHECK = 999
MAX_WORKERS = 4
MIN_TIMECODES_COUNT = 5
MIN_WORDS_AFTER_TIMECODE = 2
MAX_WORDS_AFTER_TIMECODE = 30
MIN_AVG_TIMECODE_GAP = 20
FORCE_AUTHOR = None
DEBUG = True
SITE_URL = "https://kvashe.github.io"

# ===== 1. Скомпилированная регулярка =====
TIMECODE_RE = re.compile(r'\b(?:\d{1,2}:)?\d{1,2}:\d{2}\b')

# ===== 2. Сессия requests =====
SESSION = requests.Session()

FORBIDDEN_PHRASES = [
    "хватит брать высокие ноты",
    "пой без кривляний",
    "но вот играть на ней ты явно неумеешь",
]

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================
def setup_logging(debug):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')

# ==================== АРГУМЕНТЫ КОМАНДНОЙ СТРОКИ ====================
def parse_args():
    parser = argparse.ArgumentParser(description="Парсер треклистов YouTube-трансляций")
    parser.add_argument('--api-key', default=API_KEY, help='YouTube Data API key')
    parser.add_argument('--channel', default=CHANNEL_STREAMS_URL, help='URL плейлиста трансляций')
    parser.add_argument('--db', default=DB_FILE, help='Файл базы данных')
    parser.add_argument('--output', default=OUTPUT_HTML, help='Выходной HTML-файл')
    parser.add_argument('--tracklists', default=TRACKLISTS_HTML, help='SEO-страница треклистов')
    parser.add_argument('--player', default=PLAYER_HTML, help='Страница плеера')
    parser.add_argument('--site-url', default=SITE_URL, help='URL сайта для sitemap')
    parser.add_argument('--max-workers', type=int, default=MAX_WORKERS)
    parser.add_argument('--new-streams', type=int, default=NEW_STREAMS_TO_CHECK)
    parser.add_argument('--min-timecodes', type=int, default=MIN_TIMECODES_COUNT)
    parser.add_argument('--min-words', type=int, default=MIN_WORDS_AFTER_TIMECODE)
    parser.add_argument('--max-words', type=int, default=MAX_WORDS_AFTER_TIMECODE)
    parser.add_argument('--min-gap', type=int, default=MIN_AVG_TIMECODE_GAP)
    parser.add_argument('--force-author', default=FORCE_AUTHOR)
    parser.add_argument('--debug', action='store_const', const=True, default=None)
    return parser.parse_args()

# ==================== РАБОТА С БАЗОЙ ====================
db_lock = threading.Lock()

def load_database(db_file):
    if not os.path.exists(db_file):
        return {}
    try:
        with open(db_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {item['id']: item for item in data if 'id' in item}
        elif isinstance(data, dict):
            return data
        else:
            return {}
    except (json.JSONDecodeError, Exception) as e:
        logging.warning("Ошибка чтения базы (%s). Будет создана новая.", e)
        return {}

def save_database(db, db_file):
    with db_lock:
        sorted_items = sorted(db.values(), key=lambda x: x.get("raw_date", "00000000"), reverse=True)
        tmp_file = db_file + ".tmp"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(sorted_items, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, db_file)
        except Exception as e:
            logging.error("Ошибка сохранения базы: %s", e)
            if os.path.exists(tmp_file):
                os.unlink(tmp_file)

# ==================== API YOUTUBE ====================
def get_video_comments_via_api(video_id, api_key, max_comments=3000):
    comments = []
    page_token = None
    retries = 0
    max_retries = 5

    while len(comments) < max_comments:
        params = {
            "key": api_key,
            "part": "snippet",
            "videoId": video_id,
            "maxResults": 100,
            "textFormat": "plainText",
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            resp = SESSION.get("https://www.googleapis.com/youtube/v3/commentThreads", params=params)
            if resp.status_code == 403:
                logging.warning("Quota exceeded for %s", video_id)
                break
            if resp.status_code == 429:
                retries += 1
                if retries > max_retries:
                    break
                time.sleep(2 ** retries)
                continue
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logging.error("API error for %s: %s", video_id, e)
            break

        if "error" in data:
            break

        for item in data.get("items", []):
            snippet = item["snippet"]["topLevelComment"]["snippet"]
            comments.append({
                "text": snippet.get("textDisplay", ""),
                "author": snippet.get("authorDisplayName", ""),
                "is_pinned": False,
                "published_at": snippet.get("publishedAt", "")
            })
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return comments

def get_video_duration(video_id, api_key):
    try:
        resp = SESSION.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"key": api_key, "part": "contentDetails", "id": video_id}
        ).json()
        if "items" in resp and resp["items"]:
            duration_iso = resp["items"][0]["contentDetails"]["duration"]
            match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_iso)
            if match:
                h = int(match.group(1) or 0)
                m = int(match.group(2) or 0)
                s = int(match.group(3) or 0)
                return h*3600 + m*60 + s
    except Exception as e:
        logging.warning("Не удалось получить длительность для %s: %s", video_id, e)
    return 0

# ==================== ЛОГИКА ИЗВЛЕЧЕНИЯ ТАЙМКОДОВ ====================
def get_timecode_seconds(line):
    match = TIMECODE_RE.search(line)
    if match:
        time_str = match.group(0)
        parts = [int(p) for p in time_str.split(':')]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        elif len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 99999999

def parse_timecode_range(line):
    matches = list(TIMECODE_RE.finditer(line))
    if not matches:
        return None, None, line.strip()
    start_match = matches[0]
    start_sec = get_timecode_seconds(line[start_match.start():start_match.end()])
    if len(matches) >= 2:
        between = line[start_match.end():matches[1].start()]
        if re.search(r'[-–—]', between):
            end_sec = get_timecode_seconds(line[matches[1].start():matches[1].end()])
            title = line[:start_match.start()] + line[matches[1].end():]
            title = re.sub(r'^\s*[-–—]\s*', '', title).strip()
            return start_sec, end_sec, title
    title = line[:start_match.start()] + line[start_match.end():]
    title = title.strip()
    return start_sec, None, title

def is_transcript_like(lines, min_gap):
    times = [get_timecode_seconds(l) for l in lines if get_timecode_seconds(l) != 99999999]
    if len(times) < 5:
        return False
    deltas = [times[i+1] - times[i] for i in range(len(times)-1) if times[i+1] > times[i]]
    if not deltas:
        return False
    return (sum(deltas) / len(deltas)) < min_gap

def is_good_timecode_line(line, min_words, max_words):
    match = TIMECODE_RE.search(line)
    if not match:
        return False
    after_time = line[match.end():].strip()
    if not after_time:
        return False
    clean_after = re.sub(r'[^\w\sа-яА-ЯёЁA-Za-z]', ' ', after_time)
    words = clean_after.split()
    word_count = len(words)
    effective_max = 50 if ' - ' in after_time else max_words
    special_allowed = ["титры", "интро", "начало", "конец", "финал",
                       "донаты", "розыгрыш", "чат", "сигн",
                       "вступление", "intro", "outro", "припев", "куплет"]
    lower_after = after_time.lower()
    if word_count < min_words:
        if not any(word in lower_after for word in special_allowed):
            return False
    if word_count > effective_max:
        return False
    banned_words = ["привет", "ага", "сегодня", "ладно", "понятно", "ок", "ну", "блин"]
    if lower_after in banned_words:
        return False
    return True

def extract_smart_timecodes(comments, min_timecodes, min_words, max_words, min_gap, debug):
    candidates = []
    mixed_lines = []
    mixed_authors = set()

    for comment in comments:
        text = comment.get('text', '')
        text_lower = text.lower()
        if any(phrase.lower() in text_lower for phrase in FORBIDDEN_PHRASES):
            continue

        all_lines = [line.strip() for line in text.split('\n') if TIMECODE_RE.search(line)]
        if not all_lines:
            continue

        valid_lines = [line for line in all_lines if is_good_timecode_line(line, min_words, max_words)]
        tc_count = len(valid_lines)

        if tc_count >= min_timecodes:
            if is_transcript_like(valid_lines, min_gap):
                continue

            music_score = 0
            for line in valid_lines:
                if ' - ' in line: music_score += 3
                if re.search(r'[A-Za-z]', line): music_score += 1
                if '(' in line or ')' in line: music_score += 1
                bad_words = ['стрим', 'волос', 'сигна', 'говорит', 'чат', 'вопрос',
                             'talking', 'спросить', 'умница', 'красив']
                if any(w in line.lower() for w in bad_words): music_score -= 2

            author = comment.get('author', 'Неизвестно')
            if author in ("@ajoajo701", "@mirovoy100"):
                music_score = -1_000_000

            candidates.append({
                'text': text,
                'valid_lines': valid_lines,
                'tc_count': tc_count,
                'music_score': music_score,
                'author': author,
                'published_at': comment.get('published_at', '')
            })
        else:
            mixed_lines.extend(valid_lines)
            mixed_authors.add(comment.get('author', 'Неизвестно'))

    if candidates:
        def _ts(date_str):
            try:
                return datetime.datetime.fromisoformat(date_str.replace('Z', '+00:00')).timestamp()
            except:
                return 9999999999
        best = max(candidates, key=lambda c: (c['music_score'], c['tc_count'], -_ts(c.get('published_at', ''))))
        dedup = {}
        for line in best['valid_lines']:
            sec = get_timecode_seconds(line)
            if sec not in dedup:
                dedup[sec] = line
        sorted_lines = sorted(dedup.values(), key=get_timecode_seconds)
        return sorted_lines, "tracklist", best['author']

    if mixed_lines:
        dedup_mixed = {}
        for line in mixed_lines:
            sec = get_timecode_seconds(line)
            if sec not in dedup_mixed:
                dedup_mixed[sec] = line
        unique_lines = sorted(dedup_mixed.values(), key=get_timecode_seconds)
        authors_str = ", ".join(list(mixed_authors)[:3])
        if len(mixed_authors) > 3:
            authors_str += " и др."
        return unique_lines, "mixed", authors_str

    return [], "none", ""

# ==================== ПАРСИНГ ОДНОГО ВИДЕО ====================
def parse_single_video(video_entry, api_key, db, args):
    video_id = video_entry['id']
    title = video_entry.get('title', 'No Title')
    logging.info("Парсинг: %s...", title[:60])

    comments = get_video_comments_via_api(video_id, api_key)
    timecodes, list_type, author = extract_smart_timecodes(
        comments, args.min_timecodes, args.min_words, args.max_words, args.min_gap, args.debug
    )

    if args.force_author:
        author = args.force_author

    raw_date = video_entry.get('raw_date', '00000000')
    if raw_date == '00000000':
        try:
            video_resp = SESSION.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"key": api_key, "part": "snippet", "id": video_id}
            ).json()
            if "items" in video_resp and video_resp["items"]:
                published_at = video_resp["items"][0]["snippet"]["publishedAt"]
                raw_date = published_at[:10].replace("-", "")
        except Exception as e:
            logging.warning("Не удалось получить дату для %s: %s", video_id, e)

    duration = get_video_duration(video_id, api_key)

    formatted_date = f"{raw_date[6:8]}.{raw_date[4:6]}.{raw_date[0:4]}" if len(raw_date) == 8 else "Неизвестно"

    video_data = {
        "id": video_id,
        "title": title,
        "date": formatted_date,
        "raw_date": raw_date,
        "timecodes": timecodes,
        "list_type": list_type,
        "author": author,
        "duration": duration
    }
    with db_lock:
        db[video_id] = video_data
    logging.info("Сохранено: %s... (%s) автор=%s, треков=%d, длит=%s",
                 title[:50], formatted_date, author, len(timecodes),
                 str(datetime.timedelta(seconds=duration)) if duration else "неизв")
    return True

# ==================== ГЕНЕРАЦИЯ СТРАНИЦ ====================
def generate_sitemap(site_url):
    pages = [
        {"loc": f"{site_url}/index.html", "priority": "1.0"},
        {"loc": f"{site_url}/tracklists.html", "priority": "0.8"},
        {"loc": f"{site_url}/player.html", "priority": "0.7"},
    ]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for p in pages:
        xml.append(f"  <url><loc>{p['loc']}</loc><priority>{p['priority']}</priority></url>")
    xml.append("</urlset>")
    with open("sitemap.xml", "w", encoding="utf-8") as f:
        f.write("\n".join(xml))
    logging.info("Sitemap создан: %s", os.path.abspath("sitemap.xml"))

def generate_html_report(db, site_url, output_html, tracklists_html, player_html_path):
    logging.info("Генерация HTML-отчётов и плеера...")

    # ---------- SEO-страница ----------
    streams_for_seo = [
        item for item in db.values()
        if item.get('list_type') in ('tracklist', 'mixed') and item.get('timecodes')
    ]
    streams_for_seo.sort(key=lambda x: x.get("raw_date", "00000000"), reverse=True)
    seo_lines = [
        '<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">',
        '<title>Архив треклистов Квашеной – все треклисты</title></head><body>',
        '<h1>Все треклисты трансляций Квашеной</h1>'
    ]
    for stream in streams_for_seo:
        title = html.escape(stream.get('title', 'Без названия'))
        date = html.escape(stream.get('date', ''))
        seo_lines.append(f'<h2>{title} ({date})</h2><ul>')
        for line in stream.get('timecodes', []):
            seo_lines.append(f'<li>{html.escape(line)}</li>')
        seo_lines.append('</ul>')
    seo_lines.append('</body></html>')
    with open(tracklists_html, 'w', encoding='utf-8') as f:
        f.write('\n'.join(seo_lines))
    logging.info("SEO-страница создана: %s", os.path.abspath(tracklists_html))

    # ---------- Страница плеера (стабильная версия без iOS-фиксов) ----------
    player_html = r'''<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Плеер треклистов Квашеной</title>
    <link rel="icon" href="favicon.ico" type="image/x-icon">
    <style>
        :root {
            --primary: #6366f1;
            --primary-hover: #4f46e5;
            --bg: #0b0f19;
            --card-bg: rgba(22, 28, 45, 0.8);
            --text-main: #f1f5f9;
            --text-muted: #94a3b8;
            --border: rgba(255, 255, 255, 0.08);
        }
        body {
            margin: 0; padding: 20px;
            background: #0b1020;
            color: var(--text-main);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            display: flex; flex-direction: column; align-items: center;
            min-height: 100vh;
            background-image: radial-gradient(at 80% 20%, rgba(99, 102, 241, 0.15) 0px, transparent 50%),
                              radial-gradient(at 20% 80%, rgba(244, 63, 94, 0.1) 0px, transparent 50%);
        }
        .container {
            max-width: 500px; width: 100%;
            background: var(--card-bg);
            border-radius: 20px;
            padding: 24px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.4);
            border: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            gap: 16px;
        }
        .track-info {
            display: flex;
            align-items: center;
            gap: 16px;
        }
        .thumbnail {
            width: 120px; height: 68px;
            border-radius: 10px;
            overflow: hidden;
            border: 1px solid var(--border);
            cursor: pointer;
            transition: transform 0.2s;
            flex-shrink: 0;
            background: #1e293b;
            position: relative;
        }
        .thumbnail img {
            width: 100%; height: 100%;
            object-fit: cover;
            display: none;
            transition: opacity 0.3s;
        }
        .thumbnail:hover {
            transform: scale(1.02);
            box-shadow: 0 0 12px rgba(99,102,241,0.4);
        }
        .shimmer-thumb::after {
            content: '';
            position: absolute;
            top: 0; left: 0; width: 100%; height: 100%;
            background: linear-gradient(110deg, rgba(255,255,255,0) 0%, rgba(255,255,255,0.08) 40%, rgba(255,255,255,0) 60%);
            animation: shimmerMove 1.2s infinite linear;
            pointer-events: none;
        }
        @keyframes shimmerMove {
            0% { transform: translateX(-100%); }
            100% { transform: translateX(100%); }
        }
        .track-details {
            flex: 1;
            min-width: 0;
            overflow: hidden;
        }
        .marquee {
            overflow: hidden;
            white-space: nowrap;
            position: relative;
        }
        .marquee span {
            display: inline-block;
            padding-left: 0;
            animation: none;
        }
        @keyframes marquee {
            0%   { transform: translateX(0); }
            100% { transform: translateX(-100%); }
        }
        .track-title {
            font-weight: 700;
            font-size: 1.1em;
            margin-bottom: 4px;
        }
        .track-stream {
            font-size: 0.85em;
            color: var(--text-muted);
        }
        .track-meta {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.85em;
            color: var(--text-muted);
            margin-top: 4px;
        }
        .track-integrity {
            font-size: 1.1em;
            cursor: default;
        }
        .track-author {
            font-style: italic;
        }
        .progress-container {
            width: 100%;
            height: 6px;
            background: rgba(255,255,255,0.1);
            border-radius: 4px;
            overflow: hidden;
        }
        .progress-bar {
            height: 100%;
            background: var(--primary);
            width: 0%;
            transition: width 0.1s linear;
            border-radius: 4px;
        }
        .time-info {
            display: flex;
            justify-content: space-between;
            font-size: 0.8em;
            color: var(--text-muted);
        }
        .controls {
            display: flex;
            justify-content: center;
            gap: 16px;
            align-items: center;
        }
        button {
            background: rgba(255,255,255,0.08);
            border: 1px solid rgba(255,255,255,0.12);
            color: var(--text-main);
            font-size: 1.5em;
            padding: 10px 14px;
            border-radius: 12px;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            backdrop-filter: blur(6px);
            width: 52px;
            height: 52px;
        }
        button:hover {
            background: rgba(255,255,255,0.18);
            border-color: rgba(255,255,255,0.3);
        }
        .shuffle-btn {
            background: rgba(255,255,255,0.08);
            border-color: rgba(255,255,255,0.12);
            color: var(--text-main);
        }
        .shuffle-btn.shuffled {
            background: var(--primary);
            border-color: var(--primary);
            color: white;
        }
        .volume-container {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-top: 4px;
            padding: 0 8px;
        }
        input[type=range] {
            -webkit-appearance: none;
            width: 100%;
            height: 6px;
            background: rgba(255,255,255,0.15);
            border-radius: 4px;
            outline: none;
            cursor: pointer;
        }
        input[type=range]::-webkit-slider-thumb {
            -webkit-appearance: none;
            width: 18px;
            height: 18px;
            background: var(--primary);
            border-radius: 50%;
            cursor: pointer;
            border: none;
            box-shadow: 0 0 6px rgba(99,102,241,0.5);
            transition: transform 0.1s;
        }
        input[type=range]::-webkit-slider-thumb:hover {
            transform: scale(1.1);
        }
        .playlist-link {
            text-align: center;
            margin-top: 8px;
        }
        .playlist-link a {
            color: var(--primary);
            text-decoration: none;
            font-weight: 600;
        }
        #player {
            width: 0; height: 0; visibility: hidden;
        }
    </style>
</head>
<body>
<div class="container">
    <div class="track-info">
        <a class="thumbnail shimmer-thumb" id="track-link" target="_blank">
            <img id="track-thumb" src="" alt="" onload="this.style.display='block'; this.parentElement.classList.remove('shimmer-thumb')" onerror="this.parentElement.classList.remove('shimmer-thumb')">
        </a>
        <div class="track-details">
            <div class="track-title marquee" id="track-title"><span>--</span></div>
            <div class="track-stream marquee" id="track-stream"><span></span></div>
        </div>
    </div>
    <div class="track-meta">
        <span class="track-integrity" id="track-integrity" title="">✅</span>
        <span class="track-author" id="track-author"></span>
    </div>
    <div class="progress-container">
        <div class="progress-bar" id="progress-bar"></div>
    </div>
    <div class="time-info">
        <span id="time-current">0:00</span>
        <span id="time-duration">?:??</span>
    </div>
    <div class="controls">
        <button id="prev" title="Назад">⏮️</button>
        <button id="play-pause" title="Play/Pause">▶️</button>
        <button id="next" title="Вперёд">⏭️</button>
        <button id="shuffle" class="shuffle-btn" title="Перемешать">🔀</button>
    </div>
    <div class="volume-container">
        <span style="font-size:1.5em;">🔊</span>
        <input type="range" id="volume-slider" min="0" max="100" value="100" title="Громкость">
    </div>
    <div class="playlist-link">
        <a href="index.html">← К архиву треклистов</a>
    </div>
</div>
<div id="player"></div>

<script src="https://www.youtube.com/iframe_api"></script>
<script>
    function parseTimecodeRange(line) {
        const re = /(\d{1,2}:\d{2}(?::\d{2})?)/g;
        const matches = [...line.matchAll(re)];
        if (matches.length === 0) return { start: null, end: null, title: line.trim() };
        const startStr = matches[0][1];
        const startSec = getSeconds(startStr);
        if (matches.length >= 2) {
            const between = line.substring(matches[0].index + matches[0][1].length, matches[1].index);
            if (/[-–—]/.test(between)) {
                const endSec = getSeconds(matches[1][1]);
                const title = (line.substring(0, matches[0].index) + line.substring(matches[1].index + matches[1][1].length)).trim();
                return { start: startSec, end: endSec, title: title.replace(/^[-–—]\s*/, '') };
            }
        }
        const title = (line.substring(0, matches[0].index) + line.substring(matches[0].index + matches[0][1].length)).trim();
        return { start: startSec, end: null, title: title };
    }

    function getSeconds(tc) {
        const parts = tc.split(':').map(Number);
        if (parts.length === 2) return parts[0]*60 + parts[1];
        if (parts.length === 3) return parts[0]*3600 + parts[1]*60 + parts[2];
        return 0;
    }

    let originalTracks = [];
    let tracks = [];
    let isShuffled = false;
    let currentIndex = 0;
    let player = null;
    let playerReady = false;
    let isPlaying = false;
    let checkInterval = null;
    let currentTrack = null;
    let videoDuration = 0;
    let volume = 100;

    const progressBar = document.getElementById('progress-bar');
    const timeCurrent = document.getElementById('time-current');
    const timeDuration = document.getElementById('time-duration');
    const trackAuthor = document.getElementById('track-author');
    const trackIntegrity = document.getElementById('track-integrity');
    const shuffleBtn = document.getElementById('shuffle');
    const volumeSlider = document.getElementById('volume-slider');

    function applyVolumeToPlayer() {
        if (player && playerReady) {
            player.setVolume(volume);
            if (volume == 0) {
                player.mute();
            } else {
                player.unMute();
            }
        }
    }

    volumeSlider.addEventListener('input', function() {
        volume = parseInt(this.value);
        applyVolumeToPlayer();
    });

    function setupMarquee(el, text) {
        el.innerHTML = `<span>${text}</span>`;
        const span = el.querySelector('span');
        requestAnimationFrame(() => {
            if (span.scrollWidth <= el.clientWidth) {
                span.style.animation = 'none';
                span.style.paddingLeft = '0';
            } else {
                span.style.animation = 'marquee 8s linear infinite';
                span.style.paddingLeft = '100%';
            }
        });
    }

    function formatTime(seconds) {
        if (isNaN(seconds) || seconds < 0) seconds = 0;
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = Math.floor(seconds % 60);
        if (h > 0) {
            return h + ':' + (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
        } else {
            return m + ':' + (s < 10 ? '0' : '') + s;
        }
    }

    async function init() {
        try {
            const resp = await fetch('parsed_streams_db.json');
            const db = await resp.json();
            const items = Array.isArray(db) ? db : Object.values(db);
            tracks = [];
            for (const item of items) {
                if (!item.timecodes || (item.list_type !== 'tracklist' && item.list_type !== 'mixed')) continue;
                for (const line of item.timecodes) {
                    const parsed = parseTimecodeRange(line);
                    if (parsed.start == null) continue;
                    const hasEnd = parsed.end !== null;
                    const end = parsed.end ?? parsed.start + 240;
                    tracks.push({
                        videoId: item.id,
                        start: parsed.start,
                        end: end,
                        title: parsed.title,
                        streamTitle: item.title || 'Без названия',
                        streamDate: item.date || '',
                        streamAuthor: item.author || '',
                        thumbnail: `https://img.youtube.com/vi/${item.id}/hqdefault.jpg`,
                        url: `https://www.youtube.com/watch?v=${item.id}&t=${parsed.start}s`,
                        videoDuration: item.duration || 0,
                        hasEnd: hasEnd
                    });
                }
            }
            originalTracks = [...tracks];
            updateShuffleButton();
            if (tracks.length > 0) {
                if (playerReady) {
                    playTrack(0);
                } else {
                    window._playOnReady = () => playTrack(0);
                }
            }
        } catch(e) {
            console.error('Failed to load database:', e);
            document.getElementById('track-title').textContent = 'Ошибка загрузки базы';
        }
    }

    function onYouTubeIframeAPIReady() {
        player = new YT.Player('player', {
            height: '0',
            width: '0',
            events: {
                'onReady': onPlayerReady,
                'onStateChange': onPlayerStateChange
            }
        });
    }

    function onPlayerReady(event) {
        playerReady = true;
        applyVolumeToPlayer();
        if (window._playOnReady) {
            window._playOnReady();
            window._playOnReady = null;
        } else if (tracks.length > 0 && !currentTrack) {
            playTrack(0);
        }
    }

    function playTrack(index) {
        if (!tracks.length || !player || !playerReady) return;
        currentIndex = index;
        currentTrack = tracks[currentIndex];
        setupMarquee(document.getElementById('track-title'), currentTrack.title);
        setupMarquee(document.getElementById('track-stream'), currentTrack.streamTitle + ' (' + currentTrack.streamDate + ')');
        
        if (currentTrack.hasEnd) {
            trackIntegrity.textContent = '✅';
            trackIntegrity.title = 'Треклист имеет начало и конец песни';
        } else {
            trackIntegrity.textContent = '⚠️';
            trackIntegrity.title = 'Треклист не имеет окончания песни';
        }
        trackAuthor.textContent = currentTrack.streamAuthor ? 'Автор треклиста: ' + currentTrack.streamAuthor : '';
        
        const thumbLink = document.getElementById('track-link');
        const thumbImg = document.getElementById('track-thumb');
        thumbLink.classList.add('shimmer-thumb');
        thumbImg.style.display = 'none';
        thumbImg.src = currentTrack.thumbnail;
        thumbLink.href = currentTrack.url;

        videoDuration = currentTrack.videoDuration || 0;
        updateDurationDisplay();

        player.loadVideoById({
            videoId: currentTrack.videoId,
            startSeconds: currentTrack.start
        });
    }

    function updateDurationDisplay() {
        if (videoDuration > 0) {
            timeDuration.textContent = formatTime(videoDuration);
        } else {
            timeDuration.textContent = '?:??';
        }
    }

    function startTimeCheck() {
        if (checkInterval) clearInterval(checkInterval);
        checkInterval = setInterval(() => {
            if (!player || !player.getCurrentTime || !currentTrack) return;

            if (!videoDuration || videoDuration <= 0) {
                const dur = player.getDuration();
                if (dur && dur > 0) {
                    videoDuration = dur;
                    updateDurationDisplay();
                }
            }

            const currentTime = player.getCurrentTime();
            const track = tracks[currentIndex];

            if (videoDuration > 0) {
                const progress = (currentTime / videoDuration) * 100;
                progressBar.style.width = Math.min(100, Math.max(0, progress)) + '%';
            } else {
                progressBar.style.width = '0%';
            }

            timeCurrent.textContent = formatTime(currentTime);

            if (currentTime >= track.end) {
                nextTrack();
            }
        }, 300);
    }

    function onPlayerStateChange(event) {
        if (event.data === YT.PlayerState.ENDED) {
            nextTrack();
        } else if (event.data === YT.PlayerState.PLAYING) {
            isPlaying = true;
            document.getElementById('play-pause').textContent = '⏸️';
            startTimeCheck();
        } else if (event.data === YT.PlayerState.PAUSED) {
            isPlaying = false;
            document.getElementById('play-pause').textContent = '▶️';
            if (checkInterval) clearInterval(checkInterval);
        }
    }

    function togglePlayPause() {
        if (!player || !playerReady) return;
        if (isPlaying) {
            player.pauseVideo();
        } else {
            player.playVideo();
        }
    }

    function nextTrack() {
        if (!tracks.length) return;
        currentIndex = (currentIndex + 1) % tracks.length;
        playTrack(currentIndex);
    }

    function prevTrack() {
        if (!tracks.length) return;
        currentIndex = (currentIndex - 1 + tracks.length) % tracks.length;
        playTrack(currentIndex);
    }

    function shuffleTracks() {
        if (!originalTracks.length) return;
        if (isShuffled) {
            const currentTrackId = tracks[currentIndex]?.videoId + '_' + tracks[currentIndex]?.start;
            tracks = [...originalTracks];
            const newIndex = tracks.findIndex(t => (t.videoId + '_' + t.start) === currentTrackId);
            currentIndex = newIndex !== -1 ? newIndex : 0;
            isShuffled = false;
            playTrack(currentIndex);
        } else {
            const currentTrackId = tracks[currentIndex]?.videoId + '_' + tracks[currentIndex]?.start;
            for (let i = tracks.length - 1; i > 0; i--) {
                const j = Math.floor(Math.random() * (i + 1));
                [tracks[i], tracks[j]] = [tracks[j], tracks[i]];
            }
            const newIndex = tracks.findIndex(t => (t.videoId + '_' + t.start) === currentTrackId);
            currentIndex = newIndex !== -1 ? newIndex : 0;
            isShuffled = true;
            playTrack(currentIndex);
        }
        updateShuffleButton();
    }

    function updateShuffleButton() {
        if (isShuffled) {
            shuffleBtn.textContent = '↩️';
            shuffleBtn.title = 'Восстановить порядок';
            shuffleBtn.classList.add('shuffled');
        } else {
            shuffleBtn.textContent = '🔀';
            shuffleBtn.title = 'Перемешать';
            shuffleBtn.classList.remove('shuffled');
        }
    }

    document.getElementById('play-pause').addEventListener('click', togglePlayPause);
    document.getElementById('next').addEventListener('click', nextTrack);
    document.getElementById('prev').addEventListener('click', prevTrack);
    shuffleBtn.addEventListener('click', shuffleTracks);

    if (typeof YT !== 'undefined' && YT.Player) {
        onYouTubeIframeAPIReady();
    }

    window.addEventListener('load', init);
</script>
</body>
</html>'''

    with open(player_html_path, 'w', encoding='utf-8') as f:
        f.write(player_html)
    logging.info("player.html создан: %s", os.path.abspath(player_html_path))

    # ---------- Основной index.html (оригинальный, рабочий) ----------
    safe_site_url = html.escape(site_url)
    html_template = fr"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Архив трансляций Квашеной – треклисты песен</title>
    <meta name="description" content="Полный архив музыкальных треклистов с трансляций Квашеной. Удобный поиск песен по таймкодам.">
    <meta name="keywords" content="Квашеная, треклист, трансляции, музыка, песни, архив, таймкоды">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="{safe_site_url}/index.html">
    <meta property="og:title" content="Архив треклистов Квашеной">
    <meta property="og:description" content="Все песни с трансляций – поиск по трекам и таймкодам.">
    <meta property="og:type" content="website">
    <meta property="og:url" content="{safe_site_url}/index.html">
    <link rel="icon" href="favicon.ico" type="image/x-icon">
    <style>
        :root {{
            --primary: #6366f1;
            --primary-hover: #4f46e5;
            --bg: #0b0f19;
            --card-bg: rgba(22, 28, 45, 0.6);
            --text-main: #f1f5f9;
            --text-muted: #94a3b8;
            --border: rgba(255, 255, 255, 0.08);
        }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 0 0 60px 0; color: var(--text-main); background: #0b1020; min-height: 100vh; overflow-x: hidden; -webkit-font-smoothing: antialiased; }}
        .scroll-top {{ position: fixed; bottom: 30px; right: 30px; width: 48px; height: 48px; background: rgb(23 29 61 / 28%); backdrop-filter: blur(8px); border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 50%; color: white; font-size: 24px; cursor: pointer; display: flex; align-items: center; justify-content: center; opacity: 0; visibility: hidden; transition: all 0.3s ease; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3); z-index: 1000; }}
        .scroll-top:hover {{ background: rgba(99, 102, 241, 0.9); border-color: rgba(255, 255, 255, 0.5); transform: translateY(-3px); box-shadow: 0 0 12px rgba(99, 102, 241, 0.6); }}
        .scroll-top.show {{ opacity: 1; visibility: visible; }}
        @media (max-width: 768px) {{ .scroll-top {{ bottom: 20px; right: 20px; width: 44px; height: 44px; font-size: 20px; }} }}
        .skeleton-row {{ background: linear-gradient(180deg, rgba(30, 38, 58, 0.8), rgba(22, 28, 45, 0.9)); border: 1px solid rgba(255,255,255,0.06); border-radius: 20px; padding: 24px; display: flex; flex-direction: row; gap: 24px; margin-bottom: 24px; position: relative; overflow: hidden; }}
        .skeleton-img {{ width: 160px; height: 90px; background: #1e293b; border-radius: 12px; }}
        .skeleton-content {{ flex: 1; display: flex; flex-direction: column; gap: 16px; }}
        .skeleton-title {{ width: 70%; height: 24px; background: #1e293b; border-radius: 8px; }}
        .skeleton-details {{ width: 40%; height: 20px; background: #1e293b; border-radius: 8px; }}
        .shimmer {{ position: relative; overflow: hidden; }}
        .shimmer::after {{ content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: linear-gradient(110deg, rgba(255,255,255,0) 0%, rgba(255,255,255,0.06) 40%, rgba(255,255,255,0) 60%); animation: shimmerMove 1.2s infinite linear; pointer-events: none; }}
        @keyframes shimmerMove {{ 0% {{ transform: translateX(-100%); }} 100% {{ transform: translateX(100%); }} }}
        @media (max-width: 768px) {{ .skeleton-row {{ flex-direction: column; gap: 16px; }} .skeleton-img {{ width: 100%; aspect-ratio: 16/9; height: auto; }} .skeleton-title {{ width: 85%; }} }}
        .parallax-notes {{ position: fixed; top: 0; left: 0; width: 100%; height: 100%; overflow: hidden; pointer-events: none; z-index: 0; }}
        .note {{ position: absolute; user-select: none; pointer-events: none; will-change: top; font-family: "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", sans-serif; text-shadow: 0 0 12px rgba(0,0,0,0.4); transition: top 0.1s linear; }}
        .note-content {{ display: inline-block; animation: gentleFloat 6s infinite ease-in-out; will-change: transform; }}
        @keyframes gentleFloat {{ 0% {{ transform: translateY(0px); }} 50% {{ transform: translateY(-12px); }} 100% {{ transform: translateY(0px); }} }}
        body::before {{ content: ""; position: fixed; inset: 0; z-index: -10; pointer-events: none; background-image: radial-gradient(at 80% 20%, rgba(99, 102, 241, 0.15) 0px, transparent 50%), radial-gradient(at 20% 80%, rgba(244, 63, 94, 0.1) 0px, transparent 50%); background-repeat: no-repeat; }}
        .container {{ max-width: 1000px; margin: 0 auto; padding: 0 24px; }}
        .header-panel {{ position: sticky; top: 0; background: rgba(11, 15, 25, 0.75); border-bottom: 1px solid var(--border); z-index: 100; padding: 20px 0 12px 0; margin-bottom: 40px; box-shadow: 0 10px 30px -10px rgba(0,0,0,0.5); }}
        .header-flex {{ display: flex; align-items: center; justify-content: space-between; gap: 20px; flex-wrap: wrap; }}
        .header-flex h2 {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.25rem; white-space: nowrap; }}
        .header-flex h2 span {{ white-space: nowrap; background: linear-gradient(135deg, #a5b4fc 0%, #6366f1 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
        h2 {{ color: #fff; font-size: 24px; font-weight: 800; margin: 0; display: flex; align-items: center; gap: 12px; letter-spacing: -0.5px; }}
        .search-box {{ flex-grow: 1; max-width: 400px; display: flex; flex-direction: column; align-items: flex-start; }}
        .input-wrapper {{ position: relative; width: 100%; display: flex; align-items: center; }}
        .s-input {{ width: 100%; padding: 12px 44px 12px 18px; font-size: 15px; border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; box-sizing: border-box; outline: none; transition: all 0.3s; background: rgba(15, 23, 42, 0.6); color: #fff; box-shadow: inset 0 2px 4px rgba(0,0,0,0.2); }}
        .s-input:focus {{ border-color: var(--primary); box-shadow: 0 0 0 4px rgba(99, 102, 241, 0.25), inset 0 2px 4px rgba(0,0,0,0.2); background: rgba(15, 23, 42, 0.8); }}
        #searchStats {{ color: var(--text-muted); font-size: 13px; font-weight: 600; letter-spacing: 0.3px; margin: 10px 0 4px 4px; pointer-events: none; }}
        .s-clear-btn {{ position: absolute; right: 14px; background: rgba(255, 255, 255, 0.1); border: none; width: 22px; height: 22px; border-radius: 50%; color: #94a3b8; font-size: 11px; font-weight: bold; cursor: pointer; display: none; align-items: center; justify-content: center; padding: 0; transition: all 0.2s; }}
        .s-clear-btn:hover {{ background: rgba(255, 255, 255, 0.2); color: #fff; }}
        .year-filters {{ display: flex; gap: 10px; margin-bottom: 30px; overflow-x: auto; white-space: nowrap; -webkit-overflow-scrolling: touch; padding-bottom: 8px; }}
        .year-btn {{ background: rgba(30, 41, 59, 0.5); border: 1px solid rgba(255,255,255,0.06); padding: 10px 20px; border-radius: 10px; font-weight: 600; color: var(--text-muted); cursor: pointer; transition: all 0.2s; font-size: 14px; flex-shrink: 0; }}
        .year-btn:hover {{ border-color: rgba(255,255,255,0.2); color: #fff; }}
        .year-btn.active {{ background: var(--primary); border-color: var(--primary); color: #fff; box-shadow: 0 4px 12px rgba(99, 102, 241, 0.3); }}
        .grid {{ display: flex; flex-direction: column; gap: 24px; }}
        .grid:empty {{ min-height: 60vh; }}
        .row {{ position: relative; display: flex; flex-direction: row; gap: 24px; padding: 24px; align-items: flex-start; background: linear-gradient(180deg, rgba(20,28,48,0.88), rgba(15,22,40,0.94)); border: 1px solid rgba(255,255,255,0.06); border-radius: 20px; box-shadow: 0 4px 20px rgba(0,0,0,0.15); transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1), border-color 0.3s, box-shadow 0.3s; overflow: hidden; z-index: 1; }}
        .row::before {{ content: ""; position: absolute; top: 0; left: 0; right: 0; bottom: 0; background-image: var(--bg-thumb); background-size: 120%; background-position: center; filter: blur(40px) brightness(0.25) saturate(1.4); opacity: 0.55; transition: opacity 0.3s; z-index: -1; pointer-events: none; }}
        .row * {{ position: relative; z-index: 2; }}
        .row:hover {{ transform: translateY(-2px); border-color: rgba(255, 255, 255, 0.15); box-shadow: 0 12px 30px rgba(0,0,0,0.3); }}
        .row:hover::before {{ opacity: 0.6; }}
        .v-date {{ position: absolute; top: 24px; right: 24px; color: var(--text-muted); font-size: 13px; font-weight: 700; background: rgba(15, 23, 42, 0.6); padding: 6px 12px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.06); letter-spacing: 0.5px; z-index: 3; }}
        .v-content-block {{ display: flex; flex-direction: column; gap: 16px; flex-grow: 1; padding-right: 110px; }}
        .v-link {{ text-decoration: none; flex-shrink: 0; }}
        .v-title-link {{ text-decoration: none; align-self: flex-start; }}
        .v-title-link:hover .v-title {{ color: #fff; text-shadow: 0 0 10px rgba(255,255,255,0.1); }}
        .img-container {{ position: relative; width: 160px; height: 90px; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.3); background: #151c2d; border: 1px solid rgba(255,255,255,0.05); transform: translateZ(0); }}
        .img-container::before {{ content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: #1e293b; z-index: 0; }}
        .img-container img {{ width: 101%; height: 100%; object-fit: cover; display: block; transition: transform 0.3s, opacity 0.2s; position: relative; z-index: 1; }}
        .img-container:hover img {{ transform: scale(1.05); }}
        .play-overlay {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15, 23, 42, 0.75); color: #fff; font-size: 12px; display: flex; align-items: center; justify-content: center; opacity: 0; transition: opacity 0.2s; font-weight: bold; z-index: 2; }}
        .img-container:hover .play-overlay {{ opacity: 1; }}
        .v-title {{ font-size: 18px; color: #f8fafc; font-weight: 700; line-height: 1.4; transition: color 0.2s; overflow-wrap: break-word; }}
        .v-tcs {{ width: 100%; max-width: 600px; }}
        details {{ background: rgba(15, 23, 42, 0.4); padding: 12px 18px; border-radius: 12px; border: 1px solid rgba(99, 102, 241, 0.4); transition: all 0.2s; }}
        details[open] {{ background: rgba(15, 23, 42, 0.7); border-color: rgba(99, 102, 241, 0.4); }}
        summary {{ font-weight: 600; cursor: pointer; color: #cbd5e1; outline: none; user-select: none; font-size: 14px; list-style: none; }}
        summary::-webkit-details-marker {{ display: none; }}
        .summary-flex {{ display: flex; align-items: center; justify-content: space-between; gap: 15px; }}
        .summary-flex span:first-child::before {{ content: "▼ "; font-size: 9px; color: var(--text-muted); display: inline-block; transition: transform 0.2s; margin-right: 8px; transform: rotate(-90deg); }}
        details[open] summary .summary-flex span:first-child::before {{ transform: rotate(0deg); }}
        .tc-list {{ margin-top: 16px; line-height: 1.7; max-height: 280px; overflow-y: auto; padding-right: 8px; position: relative; transition: opacity 0.3s ease, transform 0.3s ease; }}
        .tc-list::-webkit-scrollbar {{ width: 4px; }}
        .tc-list::-webkit-scrollbar-track {{ background: transparent; }}
        .tc-list::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,0.15); border-radius: 10px; }}
        .tc-list::-webkit-scrollbar-thumb:hover {{ background: rgba(255,255,255,0.3); }}
        .no-tc-block {{ background: rgba(15, 23, 42, 0.4); padding: 12px 18px; border-radius: 12px; border: 1px solid rgba(99, 102, 241, 0.4); cursor: default; display: none; }}
        .no-tc-block span {{ font-weight: 600; color: #cbd5e1; font-size: 14px; font-style: normal; }}
        .t-click {{ background: rgba(99, 102, 241, 0.15); color: #a5b4fc; padding: 3px 10px; border-radius: 6px; font-weight: 700; cursor: pointer; margin-right: 12px; display: inline-block; font-size: 13px; transition: all 0.2s; font-variant-numeric: tabular-nums; border: 1px solid rgba(99, 102, 241, 0.2); }}
        .t-click:hover {{ background: var(--primary); color: #fff; border-color: var(--primary); box-shadow: 0 0 10px rgba(99,102,241,0.4); }}
        .tc-item {{ margin-bottom: 10px; border-bottom: 1px dashed rgba(255, 255, 255, 0.15); padding-bottom: 8px; font-size: 14px; display: flex; align-items: center; }}
        .tc-item:last-of-type {{ border-bottom: none; margin-bottom: 0; padding-bottom: 0; }}
        .s-title {{ color: #e2e8f0; }}
        .badge {{ display: inline-block; padding: 5px 12px; font-size: 12px; font-weight: 600; border-radius: 8px; }}
        .badge-tracklist {{ background: rgba(16, 185, 129, 0.1); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.2); }}
        .badge-mixed {{ background: rgba(245, 158, 11, 0.1); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.2); }}
        mark {{ background: rgba(139, 92, 246, 0.35); color: #ffffff; padding: 1px 1px; border-radius: 6px; border: 1px solid rgba(196, 181, 253, 0.6); text-shadow: 0 0 6px rgba(139, 92, 246, 0.8); }}
        .hide {{ display: none; }}
        .gray {{ color: var(--text-muted); font-size: 14px; font-style: italic; }}
        .no-tcs-box {{ padding: 4px 0; }}
        .tc-author {{ font-size: 11px; color: var(--text-muted); margin-top: 10px; text-align: right; font-style: italic; opacity: 0.65; letter-spacing: 0.3px; }}
        .player-link {{
            margin-left: 20px;
            font-size: 16px;
            color: var(--primary);
            text-decoration: none;
            font-weight: 600;
            transition: color 0.2s;
        }}
        .player-link:hover {{
            color: #a5b4fc;
        }}
        @media (max-width: 768px) {{ .header-flex {{ flex-direction: column; align-items: flex-start; gap: 14px; }} .search-box {{ width: 100%; max-width: 100%; }} .row {{ flex-direction: column; gap: 16px; padding: 20px; }} .row::before {{ z-index: -1 !important; filter: blur(45px) saturate(0.9) !important; opacity: 0.5 !important; }} .v-date {{ position: static; margin-bottom: 0; font-size: 12px; align-self: flex-start; padding: 4px 8px; z-index: 2; }} .v-content-block {{ padding-right: 0; margin-top: 0; gap: 12px; z-index: 2; width: 100%; }} .v-link {{ display: block; width: 100%; z-index: 2; }} .img-container {{ width: 100%; height: auto; aspect-ratio: 16/9; border-radius: 14px; overflow: hidden; }} .img-container img {{ z-index: 1 !important; }} .play-overlay {{ display: none !important; }} .v-title {{ font-size: 16px; }} .v-tcs {{ max-width: 100%; width: 100%; }} details {{ margin: 0 -20px; border-left: none; border-right: none; border-radius: 0; padding: 12px 20px; transition: none !important; }} details[open] {{ border-bottom: none; background: rgba(15,23,42,0.8); }} .no-tc-block {{ margin: 0 -20px; border-left: none; border-right: none; border-radius: 0; padding: 12px 20px; display: none; }} .no-tc-block span {{ font-weight: 600; color: #cbd5e1; font-size: 14px; font-style: normal; }} .tc-list {{ padding-left: 8px; padding-right: 8px; max-height: 220px; overflow-y: auto; scroll-behavior: auto; -webkit-overflow-scrolling: touch; }} .t-click {{ margin-right: 6px; }} .header-panel {{ position: relative; }} summary {{ -webkit-tap-highlight-color: transparent; }} }}
    </style>
</head>
<body>
<a href="tracklists.html" style="display:none;" aria-hidden="true">Полный список треков (SEO)</a>

<div class="parallax-notes" id="parallaxNotes"></div>
<div class="header-panel">
    <div class="container header-flex">
        <h2>Архив трансляций <span>Квашеной</span><a href="player.html" class="player-link">🎵 Плеер</a></h2>
        <div class="search-box">
            <form class="input-wrapper" onsubmit="event.preventDefault();">
                <input type="text" id="sInput" class="s-input" placeholder="Поиск песни">
                <button type="button" id="sClear" class="s-clear-btn" title="Очистить поиск">✕</button>
            </form>
            <div id="searchStats">Найдено трансляций: 0</div>
        </div>
    </div>
</div>
<div class="container">
    <div id="yearFilters" class="year-filters"></div>
    <div class="grid" id="mainGrid">
        <div class="skeleton-row shimmer"><div class="skeleton-img shimmer"></div><div class="skeleton-content"><div class="skeleton-title shimmer"></div><div class="skeleton-details shimmer"></div></div></div>
        <div class="skeleton-row shimmer"><div class="skeleton-img shimmer"></div><div class="skeleton-content"><div class="skeleton-title shimmer"></div><div class="skeleton-details shimmer"></div></div></div>
        <div class="skeleton-row shimmer"><div class="skeleton-img shimmer"></div><div class="skeleton-content"><div class="skeleton-title shimmer"></div><div class="skeleton-details shimmer"></div></div></div>
    </div>
</div>
<button class="scroll-top" id="scrollTopBtn" aria-label="Наверх">↑</button>
<script>
(function() {{
    const notesContainer = document.getElementById('parallaxNotes');
    if (!notesContainer) return;
    const noteSymbols = ['♪', '♫', '♩', '🎵', '🎶', '𝄞', '♬', '🎙️', '🎸', '🎹'];
    const notesCount = 10;
    const notes = [];
    for (let i = 0; i < notesCount; i++) {{
        const noteDiv = document.createElement('div');
        noteDiv.className = 'note';
        const symbol = noteSymbols[Math.floor(Math.random() * noteSymbols.length)];
        const size = Math.floor(Math.random() * 130) + 50;
        const left = Math.random() * 100;
        const top = Math.random() * 100;
        const opacity = Math.random() * 0.4 + 0.2;
        const rotation = Math.random() * 360;
        const parallaxFactor = 0.2 + Math.random() * 0.6;
        const contentSpan = document.createElement('span');
        contentSpan.className = 'note-content';
        contentSpan.textContent = symbol;
        contentSpan.style.fontSize = size + 'px';
        contentSpan.style.opacity = opacity;
        contentSpan.style.color = `rgba(255, 255, 255, ${{opacity * 0.9}})`;
        const animDuration = 4 + Math.random() * 6;
        contentSpan.style.animation = `gentleFloat ${{animDuration}}s infinite ease-in-out`;
        contentSpan.style.animationDelay = `${{Math.random() * 3}}s`;
        noteDiv.appendChild(contentSpan);
        noteDiv.style.left = left + '%';
        noteDiv.style.top = top + '%';
        noteDiv.style.transform = `rotate(${{rotation}}deg)`;
        notesContainer.appendChild(noteDiv);
        notes.push({{ element: noteDiv, baseTop: parseFloat(top), parallaxFactor }});
    }}
    let ticking = false;
    function updateNotesPosition() {{
        const scrollY = window.scrollY;
        const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
        const scrollProgress = maxScroll > 0 ? scrollY / maxScroll : 0;
        for (let n of notes) {{
            const shiftPercent = (scrollProgress - 0.5) * n.parallaxFactor * 16;
            let newTop = n.baseTop + shiftPercent;
            newTop = Math.min(Math.max(newTop, -5), 105);
            n.element.style.top = newTop + '%';
        }}
        ticking = false;
    }}
    window.addEventListener('scroll', () => {{ if (!ticking) {{ requestAnimationFrame(updateNotesPosition); ticking = true; }} }});
    window.addEventListener('resize', () => updateNotesPosition());
    updateNotesPosition();
}})();

function removeSpecificEmojis(str) {{
    if (typeof Intl !== 'undefined' && Intl.Segmenter) {{
        const segmenter = new Intl.Segmenter('en', {{ granularity: 'grapheme' }});
        const segments = [...segmenter.segment(str)];
        let result = '';
        for (const seg of segments) {{
            const grapheme = seg.segment;
            const isEmoji = /\p{{Emoji}}/u.test(grapheme) && !/[\p{{N}}\p{{L}}]/u.test(grapheme);
            if (!isEmoji) {{
                result += grapheme;
            }}
        }}
        return result;
    }} else {{
        return str.replace(/[\u{{1F600}}-\u{{1F64F}}\u{{1F300}}-\u{{1F5FF}}\u{{1F680}}-\u{{1F6FF}}\u{{1F1E0}}-\u{{1F1FF}}\u{{2600}}-\u{{26FF}}\u{{2700}}-\u{{27BF}}\u{{1F900}}-\u{{1F9FF}}\u{{1FA00}}-\u{{1FA6F}}\u{{1FA70}}-\u{{1FAFF}}]/gu, '');
    }}
}}

function normalize(str) {{
    return str.toLowerCase()
              .replace(/ë/g, 'e')
              .replace(/[^a-zа-яё0-9]/g, '');
}}

const SYNONYMS = {{
    "noizemc": ["noizemc", "noizemc", "нойзмс", "нойзмс", "noize", "нойз"],
    "rammstein": ["rammstein", "раммштайн"],
    "корольишут": ["корольишут", "киш"],
    "витас": ["витас", "vitas"],
    "ladygaga": ["ladygaga", "ледигага"],
    "максим": ["максим", "макsим"],
    "fleur": ["fleur", "flëur"],
    "nautiluspompilius": ["nautiluspompilius", "наутилуспомпилиус", "pompilius", "nautilus", "наутилус", "помпилиус"],
    "океанэлзи": ["океанэлзи", "элзи", "океанэльзы", "эльзы", "океанельзи", "ельзи"],
    "iowa": ["iowa", "айова"]
}};

const variantToCanon = new Map();
for (const [canon, variants] of Object.entries(SYNONYMS)) {{
    for (const v of variants) {{
        variantToCanon.set(v, canon);
    }}
}}

function getSearchVariants(query) {{
    const normQuery = normalize(query);
    if (variantToCanon.has(normQuery)) {{
        const canon = variantToCanon.get(normQuery);
        return SYNONYMS[canon];
    }}
    return [normQuery];
}}

function matchesWithVariants(textNorm, variants) {{
    for (let v of variants) {{
        const normVariant = normalize(v);
        if (textNorm.includes(normVariant)) return true;
    }}
    return false;
}}

function escapeHtml(str) {{
    const div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}}

function highlightFirstMatch(escapedText, variants) {{
    if (!variants || variants.length === 0) return escapedText;
    const normEscaped = normalize(escapedText);
    let bestMatch = null;
    let bestIndex = Infinity;
    for (let v of variants) {{
        const normV = normalize(v);
        const idx = normEscaped.indexOf(normV);
        if (idx !== -1 && idx < bestIndex) {{
            bestIndex = idx;
            bestMatch = normV;
        }}
    }}
    if (bestMatch === null) return escapedText;
    let origIdx = 0, normIdx = 0;
    while (normIdx < bestIndex && origIdx < escapedText.length) {{
        const ch = escapedText[origIdx];
        const nch = normalize(ch);
        if (nch.length > 0) normIdx++;
        origIdx++;
    }}
    const start = origIdx;
    while (normIdx < bestIndex + bestMatch.length && origIdx < escapedText.length) {{
        const ch = escapedText[origIdx];
        const nch = normalize(ch);
        if (nch.length > 0) normIdx++;
        origIdx++;
    }}
    const end = origIdx;
    return escapedText.substring(0, start) + '<mark>' + escapedText.substring(start, end) + '</mark>' + escapedText.substring(end);
}}

let streamsData = [];
let songsDB = {{}};
let searchIndex = [];
let activeYear = 'all';
let allRows = [];

function getTimecodeSeconds(line) {{
    const match = line.match(/(\d{{1,2}}:\d{{2}}(?::\d{{2}})?)/);
    if (!match) return 99999999;
    const parts = match[1].split(':').map(Number);
    if (parts.length === 2) return parts[0]*60 + parts[1];
    if (parts.length === 3) return parts[0]*3600 + parts[1]*60 + parts[2];
    return 99999999;
}}

function normalizeTimecode(tc) {{
    const parts = tc.split(':').map(Number);
    if (parts.length === 2) return `00:${{parts[0].toString().padStart(2,'0')}}:${{parts[1].toString().padStart(2,'0')}}`;
    if (parts.length === 3) return parts.map(p => p.toString().padStart(2,'0')).join(':');
    return tc;
}}

async function loadDatabase() {{
    if (streamsData.length > 0) return;
    try {{
        const response = await fetch('parsed_streams_db.json');
        if (!response.ok) throw new Error('Файл базы не найден');
        let rawData = await response.json();
        rawData.sort((a,b) => (b.raw_date || '00000000').localeCompare(a.raw_date || '00000000'));
        streamsData = rawData.filter(entry => entry.list_type === 'tracklist' || entry.list_type === 'mixed');
        songsDB = {{}};
        searchIndex = [];
        streamsData.forEach(entry => {{
            const vId = entry.id;
            const timecodes = entry.timecodes || [];
            const sorted = timecodes.slice().sort((a,b) => getTimecodeSeconds(a) - getTimecodeSeconds(b));
            const tracks = sorted.map(line => {{
                const cleanedLine = line.replace(
                    /(\d{{1,2}}:\d{{2}}(?::\d{{2}})?)\s*[-–—]\s*\d{{1,2}}:\d{{2}}(?::\d{{2}})?/,
                    '$1'
                );
                const match = cleanedLine.match(/(\d{{1,2}}:\d{{2}}(?::\d{{2}})?)/);
                if (match) {{
                    let s = cleanedLine.replace(match[1], '').trim();
                    s = s.replace(/^[-–—]\s*/, '');
                    s = removeSpecificEmojis(s);
                    return {{ t: match[1], s: s }};
                }} else {{
                    let s = removeSpecificEmojis(cleanedLine);
                    return {{ s: s }};
                }}
            }});
            songsDB[vId] = {{ tracks, author: entry.author || '' }};
            tracks.forEach(tr => {{
                const norm = normalize(tr.s || '');
                searchIndex.push({{ id: vId, text: tr.s, norm: norm }});
            }});
        }});
    }} catch(e) {{ console.error('Ошибка загрузки базы:', e); streamsData = []; }}
}}

function renderStreamHTML(stream) {{
    const vId = stream.id;
    const title = escapeHtml(stream.title || 'Без названия');
    const date = escapeHtml(stream.date || 'Неизвестно');
    const rawYear = (stream.raw_date || '0000').substring(0,4);
    const timecodes = stream.timecodes || [];
    const listType = stream.list_type || 'none';
    const hasTracks = timecodes.length > 0;
    const badgeClass = `badge-${{listType}}`;
    const badgeText = listType === 'tracklist' ? 'Готовый трек-лист' : (listType === 'mixed' ? 'Сборный список' : '');
    const tcsHTML = hasTracks ? `<details><summary><div class="summary-flex"><span>Треклист ${{timecodes.length}}</span><span class="badge ${{badgeClass}}">${{badgeText}}</span></div></summary><div class="tc-list"></div></details>` : '<div class="no-tc-block"><span>Треклист не найден</span></div>';
    return `<div class="row" data-id="${{vId}}" data-year="${{rawYear}}" style="--bg-thumb: url('https://img.youtube.com/vi/${{vId}}/hqdefault.jpg');"><span class="v-date">${{date}}</span><a class="v-link" href="https://www.youtube.com/watch?v=${{vId}}" target="_blank"><div class="img-container"><img loading="lazy" decoding="async" src="https://img.youtube.com/vi/${{vId}}/hqdefault.jpg" alt="" onerror="this.style.opacity='0';"><span class="play-overlay">▶ Смотреть</span></div></a><div class="v-content-block"><a class="v-title-link" href="https://www.youtube.com/watch?v=${{vId}}" target="_blank"><span class="v-title">${{title}}</span></a><div class="v-tcs">${{tcsHTML}}</div></div></div>`;
}}

function animateContainer(container) {{
    container.style.transition = 'none';
    container.style.opacity = '0';
    container.style.transform = 'translateY(-10px)';
    void container.offsetHeight;
    container.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
    container.style.opacity = '1';
    container.style.transform = 'translateY(0)';
}}

function renderAllStreams() {{
    const grid = document.getElementById('mainGrid');
    grid.innerHTML = streamsData.map(renderStreamHTML).join('');
    allRows = [...grid.querySelectorAll('.row')];
    document.querySelectorAll('details').forEach(details => {{
        details.addEventListener('toggle', function() {{
            const container = this.querySelector('.tc-list');
            if (!container) return;
            if (this.open) {{
                const row = this.closest('.row');
                const vId = row.getAttribute('data-id');
                const filter = searchInput.value.toLowerCase().trim();
                if (container.children.length === 0) renderTracklist(vId, container, filter);
                animateContainer(container);
                if (filter) {{
                    setTimeout(() => {{
                        const mark = container.querySelector('mark');
                        if (mark) {{
                            const item = mark.closest('.tc-item');
                            if (item) container.scrollTop = item.offsetTop - container.offsetTop - 10;
                        }}
                    }}, 50);
                }}
            }} else {{
                container.style.transition = 'none';
                container.style.opacity = '0';
                container.style.transform = 'translateY(-10px)';
            }}
        }});
    }});
}}

function initYearFilters() {{
    const yearsSet = new Set();
    allRows.forEach(row => {{ const y = row.getAttribute('data-year'); if(y && y !== '0000') yearsSet.add(y); }});
    const sortedYears = Array.from(yearsSet).sort().reverse();
    const container = document.getElementById('yearFilters');
    let html = '<button class="year-btn active" data-year="all">Все годы</button>';
    sortedYears.forEach(y => html += `<button class="year-btn" data-year="${{y}}">${{y}} года</button>`);
    container.innerHTML = html;
    container.addEventListener('click', (e) => {{
        if(e.target.classList.contains('year-btn')) {{
            document.querySelectorAll('.year-btn').forEach(b => b.classList.remove('active'));
            e.target.classList.add('active');
            activeYear = e.target.getAttribute('data-year');
            executeSearch(searchInput.value.toLowerCase().trim());
        }}
    }});
}}

function renderTracklist(vId, container, filter) {{
    const tracks = songsDB[vId]?.tracks || [];
    let html = '';
    if (filter) {{
        const variants = getSearchVariants(filter);
        tracks.forEach(tr => {{
            let sText = tr.s;
            sText = escapeHtml(sText);
            const normText = normalize(sText);
            if (matchesWithVariants(normText, variants)) {{
                sText = highlightFirstMatch(sText, variants);
            }}
            const displayedTime = tr.t ? normalizeTimecode(tr.t) : '';
            html += tr.t ? `<div class="tc-item"><span class="t-click" data-time="${{tr.t}}">${{displayedTime}}</span><span class="s-title">${{sText}}</span></div>` : `<div class="tc-item"><span class="s-title">${{sText}}</span></div>`;
        }});
    }} else {{
        tracks.forEach(tr => {{
            const displayedTime = tr.t ? normalizeTimecode(tr.t) : '';
            const safeText = escapeHtml(tr.s);
            html += tr.t ? `<div class="tc-item"><span class="t-click" data-time="${{tr.t}}">${{displayedTime}}</span><span class="s-title">${{safeText}}</span></div>` : `<div class="tc-item"><span class="s-title">${{safeText}}</span></div>`;
        }});
    }}
    const author = songsDB[vId]?.author;
    if (author && author.trim() !== '') html += `<div class="tc-author">Автор треклиста: ${{escapeHtml(author)}}</div>`;
    container.innerHTML = html;
}}

const searchInput = document.getElementById('sInput');
const clearBtn = document.getElementById('sClear');
const statsEl = document.getElementById('searchStats');

async function executeSearch(filter) {{
    if (allRows.length === 0) return;
    let visibleCount = 0;
    const variants = filter ? getSearchVariants(filter) : [];
    clearBtn.style.display = filter ? 'flex' : 'none';
    allRows.forEach(row => {{
        row.style.display = 'none';
        const details = row.querySelector('details');
        if (details) {{
            details.open = false;
            const tcList = details.querySelector('.tc-list');
            if (tcList) {{ tcList.style.transition = 'none'; tcList.style.opacity = '0'; tcList.style.transform = 'translateY(-10px)'; tcList.innerHTML = ''; }}
        }}
    }});
    if (!filter) {{
        allRows.forEach(row => {{ const rowYear = row.getAttribute('data-year'); if (activeYear === 'all' || rowYear === activeYear) {{ row.style.display = ''; visibleCount++; }} }});
        statsEl.textContent = 'Найдено трансляций: ' + visibleCount;
        return;
    }}
    const matchedIds = new Set();
    searchIndex.forEach(item => {{
        if (matchesWithVariants(item.norm, variants)) matchedIds.add(item.id);
    }});
    const isDesktop = window.innerWidth > 768;
    allRows.forEach(row => {{
        const vId = row.getAttribute('data-id');
        const rowYear = row.getAttribute('data-year');
        if (activeYear !== 'all' && rowYear !== activeYear) return;
        if (!matchedIds.has(vId)) return;
        row.style.display = '';
        visibleCount++;
        if (isDesktop && filter.length >= 2) {{
            const details = row.querySelector('details');
            const tcList = row.querySelector('.tc-list');
            if (details && tcList && tcList.children.length === 0) {{
                details.open = true;
                renderTracklist(vId, tcList, filter);
                animateContainer(tcList);
                setTimeout(() => {{
                    const mark = tcList.querySelector('mark');
                    if (mark) {{
                        const item = mark.closest('.tc-item');
                        if (item) tcList.scrollTop = item.offsetTop - tcList.offsetTop - 10;
                    }}
                }}, 50);
            }}
        }}
    }});
    statsEl.textContent = 'Найдено трансляций: ' + visibleCount;
}}

const scrollBtn = document.getElementById('scrollTopBtn');
window.addEventListener('scroll', () => {{ if (window.scrollY > 300) scrollBtn.classList.add('show'); else scrollBtn.classList.remove('show'); }});
scrollBtn.addEventListener('click', () => window.scrollTo({{ top: 0, behavior: 'smooth' }}));

document.addEventListener('DOMContentLoaded', async () => {{
    await loadDatabase();
    renderAllStreams();
    initYearFilters();
    executeSearch('');
}});

document.getElementById('mainGrid').addEventListener('click', (e) => {{
    if (e.target.classList.contains('t-click')) {{
        e.preventDefault();
        const time = e.target.getAttribute('data-time');
        const vId = e.target.closest('.row').getAttribute('data-id');
        const parts = time.split(':').map(Number);
        const secs = parts.length === 2 ? parts[0]*60 + parts[1] : parts[0]*3600 + parts[1]*60 + parts[2];
        window.open(`https://www.youtube.com/watch?v=${{vId}}&t=${{secs}}s`, '_blank');
    }}
}});

let debounceTimer;
searchInput.addEventListener('input', () => {{ clearTimeout(debounceTimer); debounceTimer = setTimeout(() => executeSearch(searchInput.value.toLowerCase().trim()), 700); }});
clearBtn.addEventListener('click', () => {{ searchInput.value = ''; clearBtn.style.display = 'none'; executeSearch(''); }});
</script>
</body>
</html>"""

    html_content = "\n".join(line.rstrip() for line in html_template.splitlines() if line.strip())
    html_content = re.sub(r'>\s+<', '><', html_content)
    html_content = re.sub(r'\n+', '\n', html_content)

    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html_content)
    logging.info("HTML обновлён: %s", os.path.abspath(output_html))

    generate_sitemap(site_url)

# ==================== ГЛАВНАЯ ФУНКЦИЯ ====================
def run_parser():
    args = parse_args()
    debug_mode = args.debug if args.debug is not None else DEBUG
    setup_logging(debug_mode)

    api_key = args.api_key
    if not api_key:
        logging.error("API-ключ YouTube не указан. Задайте --api-key или переменную YOUTUBE_API_KEY")
        sys.exit(1)

    db = load_database(args.db)
    is_first_run = len(db) == 0

    flat_opts = {
        'playlistend': None if is_first_run else args.new_streams,
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
    }
    try:
        with YoutubeDL(flat_opts) as ydl:
            logging.info("Получение списка трансляций через yt-dlp...")
            channel_data = ydl.extract_info(args.channel, download=False)
            if not channel_data or 'entries' not in channel_data:
                logging.error("Не удалось получить список стримов.")
                return

            videos_to_parse = []
            videos_to_fix_date = []
            for entry in channel_data['entries']:
                if not entry:
                    continue
                video_id = entry.get('id')
                if not video_id:
                    continue
                if video_id in db and db[video_id].get('list_type') == 'tracklist':
                    if db[video_id].get('raw_date', '00000000') == '00000000':
                        raw_date = entry.get('upload_date', '00000000')
                        videos_to_fix_date.append((video_id, raw_date))
                    continue
                raw_date = entry.get('upload_date', '00000000')
                videos_to_parse.append({
                    'id': video_id,
                    'title': entry.get('title', 'Без названия'),
                    'raw_date': raw_date
                })

        if videos_to_fix_date:
            logging.info("Восстановление дат для %d существующих треклистов...", len(videos_to_fix_date))
            for vid, raw_date in videos_to_fix_date:
                if raw_date == '00000000':
                    try:
                        video_resp = SESSION.get(
                            "https://www.googleapis.com/youtube/v3/videos",
                            params={"key": api_key, "part": "snippet", "id": vid}
                        ).json()
                        if "items" in video_resp and video_resp["items"]:
                            published_at = video_resp["items"][0]["snippet"]["publishedAt"]
                            raw_date = published_at[:10].replace("-", "")
                    except Exception as e:
                        logging.warning("Не удалось восстановить дату для %s: %s", vid, e)
                if raw_date != '00000000':
                    with db_lock:
                        if vid in db:
                            db[vid]['raw_date'] = raw_date
                            db[vid]['date'] = f"{raw_date[6:8]}.{raw_date[4:6]}.{raw_date[0:4]}"
                            logging.info("Дата обновлена для %s: %s", vid, db[vid]['date'])
            save_database(db, args.db)

        if videos_to_parse:
            logging.info("Парсинг %d видео...", len(videos_to_parse))

            def worker(video):
                parse_single_video(video, api_key, db, args)

            with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
                executor.map(worker, videos_to_parse)

            save_database(db, args.db)

        logging.info("Генерация отчётов и плеера...")
        generate_html_report(db, args.site_url, args.output, args.tracklists, args.player)

    except Exception as e:
        logging.exception("Критическая ошибка")
        sys.exit(1)

if __name__ == "__main__":
    run_parser()