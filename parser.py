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

    # ---------- Страница плеера ----------
    player_html = r'''<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <title>Плеер треклистов Квашеной</title>
    <link rel="icon" href="favicon.ico" type="image/x-icon">
    <style>
        :root {
            --primary: #6366f1;
            --primary-hover: #4f46e5;
            --primary-glow: rgba(99, 102, 241, 0.3);
            --bg: #0b0f19;
            --card-bg: rgba(22, 28, 45, 0.7);
            --text-main: #f1f5f9;
            --text-secondary: #e2e8f0;
            --text-muted: #94a3b8;
            --border: rgba(255, 255, 255, 0.08);
            --progress-bg: rgba(255, 255, 255, 0.1);
            --card-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
            --backdrop-blur: blur(16px);
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
            background: #0b1020;
            color: var(--text-main);
            background-image: 
                radial-gradient(at 80% 20%, rgba(99, 102, 241, 0.1) 0px, transparent 50%),
                radial-gradient(at 20% 80%, rgba(244, 63, 94, 0.05) 0px, transparent 50%);
        }
        .container {
            width: 100%;
            max-width: 420px;
            background: var(--card-bg);
            backdrop-filter: var(--backdrop-blur);
            -webkit-backdrop-filter: var(--backdrop-blur);
            border-radius: 32px;
            padding: 32px 28px;
            box-shadow: var(--card-shadow);
            border: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            gap: 28px;
            position: relative;
            overflow: hidden;
            transition: background 0.8s ease, border-color 0.8s ease;
        }
        .container::before {
            content: '';
            position: absolute;
            top: -50%;
            left: -50%;
            width: 200%;
            height: 200%;
            background: var(--bg-image) center/cover;
            filter: blur(80px) brightness(0.15) saturate(2);
            opacity: 0.6;
            z-index: -1;
            animation: slowPulse 8s ease-in-out infinite;
            transform: scale(1.1);
        }
        @keyframes slowPulse {
            0%, 100% { opacity: 0.5; transform: scale(1.1); }
            50% { opacity: 0.7; transform: scale(1.15); }
        }
        .artwork-container {
            width: 100%;
            aspect-ratio: 16 / 9;
            border-radius: 20px;
            overflow: hidden;
            position: relative;
            box-shadow: 0 12px 40px rgba(0, 0, 0, 0.5);
            cursor: pointer;
            transition: transform 0.4s ease, box-shadow 0.4s;
            z-index: 1;
            background: #1e293b;
        }
        .artwork-container:hover {
            transform: scale(1.02);
            box-shadow: 0 16px 48px rgba(0, 0, 0, 0.6);
        }
        .artwork-container.playing {
            animation: gentleRotate 8s ease-in-out infinite alternate;
        }
        @keyframes gentleRotate {
            0% { transform: scale(1) rotate(0deg); }
            25% { transform: scale(1.01) rotate(1deg); }
            75% { transform: scale(1.01) rotate(-1deg); }
            100% { transform: scale(1) rotate(0deg); }
        }
        .artwork-image {
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
            position: relative;
            z-index: 1;
        }
        .artwork-placeholder {
            position: absolute;
            inset: 0;
            background: #1e293b;
            z-index: 0;
        }
        .artwork-overlay {
            position: absolute;
            inset: 0;
            background: rgba(0, 0, 0, 0.3);
            display: flex;
            align-items: center;
            justify-content: center;
            opacity: 0;
            transition: opacity 0.3s ease;
            z-index: 2;
        }
        .artwork-container:hover .artwork-overlay { opacity: 1; }
        .play-icon-overlay {
            width: 64px;
            height: 64px;
            background: rgba(255, 255, 255, 0.2);
            backdrop-filter: blur(8px);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
        }
        .track-info {
            text-align: center;
            display: flex;
            flex-direction: column;
            gap: 6px;
            overflow: hidden;
        }
        .track-title-row {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            min-height: 32px;
        }
        .track-title {
            font-weight: 700;
            font-size: 1.25rem;
            color: var(--text-main);
            overflow: hidden;
            max-width: 100%;
            position: relative;
            width: 100%;
            white-space: nowrap;
            text-overflow: ellipsis;
        }
        .track-title.marquee {
            text-overflow: clip;
        }
        .track-title-inner {
            display: inline-block;
            white-space: nowrap;
        }
        .track-title.marquee .track-title-inner {
            animation: marquee var(--marquee-duration, 10s) linear infinite;
        }
        .stream-info {
            font-size: 0.85rem;
            color: var(--text-muted);
            font-weight: 500;
            overflow: hidden;
            position: relative;
            width: 100%;
            white-space: nowrap;
            text-overflow: ellipsis;
        }
        .stream-info.marquee {
            text-overflow: clip;
        }
        .stream-info-inner {
            display: inline-block;
            white-space: nowrap;
        }
        .stream-info.marquee .stream-info-inner {
            animation: marquee var(--marquee-duration, 10s) linear infinite;
        }
        @keyframes marquee {
            0% { transform: translateX(0); }
            100% { transform: translateX(-50%); }
        }
        .playing-indicator {
            display: flex;
            align-items: center;
            gap: 3px;
            height: 20px;
            opacity: 0;
            transition: opacity 0.3s;
            flex-shrink: 0;
        }
        .playing-indicator.active { opacity: 1; }
        .eq-bar {
            width: 3px;
            background: var(--primary);
            border-radius: 3px;
            animation: eqAnim 1.2s infinite ease-in-out;
        }
        .eq-bar:nth-child(1) { height: 12px; animation-delay: 0s; }
        .eq-bar:nth-child(2) { height: 18px; animation-delay: 0.2s; }
        .eq-bar:nth-child(3) { height: 14px; animation-delay: 0.4s; }
        .eq-bar:nth-child(4) { height: 8px; animation-delay: 0.6s; }
        @keyframes eqAnim {
            0%, 100% { transform: scaleY(1); }
            50% { transform: scaleY(0.4); }
        }
        .track-meta {
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 12px;
            font-size: 0.8rem;
            color: var(--text-muted);
        }
        .track-author { font-style: italic; }
        .track-integrity {
            cursor: help;
            font-size: 1.1em;
        }
        .progress-section {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        .progress-container {
            position: relative;
            width: 100%;
            height: 6px;
            background: var(--progress-bg);
            border-radius: 6px;
            cursor: pointer;
            overflow: visible;
        }
        .progress-bg {
            position: absolute;
            inset: 0;
            border-radius: 6px;
            background: rgba(255, 255, 255, 0.05);
        }
        .progress-fill {
            position: absolute;
            top: 0;
            left: 0;
            height: 100%;
            background: linear-gradient(90deg, var(--primary), var(--primary-hover));
            border-radius: 6px;
            width: 0%;
            transition: width 0.1s linear;
            box-shadow: 0 0 12px var(--primary-glow);
            z-index: 1;
        }
        .progress-thumb {
            position: absolute;
            top: 50%;
            transform: translate(-50%, -50%);
            width: 16px;
            height: 16px;
            background: white;
            border-radius: 50%;
            opacity: 0;
            transition: opacity 0.2s;
            z-index: 2;
            pointer-events: none;
        }
        .progress-container:hover .progress-thumb { opacity: 1; }
        .progress-hover-time {
            position: absolute;
            top: -30px;
            transform: translateX(-50%);
            background: rgba(0, 0, 0, 0.8);
            color: white;
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 12px;
            white-space: nowrap;
            opacity: 0;
            pointer-events: none;
            z-index: 3;
        }
        .progress-container:hover .progress-hover-time.active { opacity: 1; }
        .time-info {
            display: flex;
            justify-content: space-between;
            font-size: 0.75rem;
            font-weight: 500;
            color: var(--text-muted);
            font-variant-numeric: tabular-nums;
        }
        .controls {
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 24px;
        }
        .ctrl-btn {
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.08);
            color: var(--text-main);
            width: 48px;
            height: 48px;
            border-radius: 50%;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.25s ease;
            flex-shrink: 0;
        }
        .ctrl-btn:hover {
            background: rgba(255, 255, 255, 0.14);
            transform: scale(1.05);
        }
        .ctrl-btn:active { transform: scale(0.95); }
        .ctrl-btn.play-btn {
            width: 64px;
            height: 64px;
            background: var(--primary);
            border-color: var(--primary);
            box-shadow: 0 8px 24px var(--primary-glow);
            color: white;
        }
        .ctrl-btn.play-btn:hover {
            background: var(--primary-hover);
            transform: scale(1.08);
        }
        .ctrl-btn.shuffle-active {
            background: var(--primary);
            border-color: var(--primary);
            color: white;
        }
        .ctrl-btn.repeat-active {
            background: var(--primary);
            border-color: var(--primary);
            color: white;
        }
        .ctrl-btn svg { width: 22px; height: 22px; }
        .ctrl-btn.play-btn svg { width: 28px; height: 28px; }
        .volume-section {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 0 4px;
        }
        .volume-icon {
            color: var(--text-muted);
            display: flex;
            align-items: center;
            flex-shrink: 0;
            cursor: pointer;
        }
        .volume-icon:hover { color: var(--text-main); }
        input[type="range"] {
            -webkit-appearance: none;
            width: 100%;
            height: 5px;
            background: var(--progress-bg);
            border-radius: 5px;
            outline: none;
            cursor: pointer;
        }
        input[type="range"]::-webkit-slider-thumb {
            -webkit-appearance: none;
            width: 16px;
            height: 16px;
            background: var(--primary);
            border-radius: 50%;
            cursor: pointer;
            border: 2px solid white;
        }
        .playlist-section {
            background: rgba(255, 255, 255, 0.02);
            border-radius: 16px;
            border: 1px solid rgba(255, 255, 255, 0.06);
            overflow: hidden;
        }
        .playlist-header {
            padding: 14px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            cursor: pointer;
            user-select: none;
            font-weight: 600;
            font-size: 0.9rem;
            color: var(--text-secondary);
        }
        .playlist-header:hover { color: var(--text-main); }
        .playlist-header svg {
            transition: transform 0.3s ease;
            width: 16px;
            height: 16px;
            opacity: 0.7;
        }
        .playlist-section.open .playlist-header svg { transform: rotate(180deg); }
        .track-list {
            max-height: 220px;
            overflow-y: auto;
            padding: 0 12px 12px;
            display: none;
            scroll-behavior: smooth;
        }
        .playlist-section.open .track-list { display: block; }
        .track-list::-webkit-scrollbar { width: 4px; }
        .track-list::-webkit-scrollbar-track { background: transparent; }
        .track-list::-webkit-scrollbar-thumb {
            background: rgba(255,255,255,0.15);
            border-radius: 10px;
        }
        .track-item {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 10px 12px;
            border-radius: 10px;
            cursor: pointer;
            transition: background 0.2s;
            font-size: 0.85rem;
            color: var(--text-muted);
        }
        .track-item:hover {
            background: rgba(255, 255, 255, 0.06);
            color: var(--text-secondary);
        }
        .track-item.active {
            background: rgba(99, 102, 241, 0.15);
            color: white;
            font-weight: 500;
            box-shadow: inset 3px 0 0 var(--primary);
        }
        .track-item .tc-time {
            font-variant-numeric: tabular-nums;
            font-weight: 600;
            font-size: 0.8rem;
            color: var(--primary);
            min-width: 55px;
        }
        .track-item.active .tc-time { color: white; }
        .track-item .tc-title {
            flex: 1;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .track-item .tc-duration {
            font-size: 0.75rem;
            opacity: 0.6;
            font-variant-numeric: tabular-nums;
        }
        .nav-link {
            text-align: center;
            margin-top: -8px;
        }
        .nav-link a {
            color: var(--text-muted);
            text-decoration: none;
            font-size: 0.85rem;
            font-weight: 500;
            transition: color 0.2s;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }
        .nav-link a:hover { color: var(--primary); }
        @media (max-width: 480px) {
            .container { border-radius: 24px; padding: 24px 20px; gap: 24px; }
            .artwork-container { border-radius: 16px; }
            .controls { gap: 16px; }
            .ctrl-btn { width: 44px; height: 44px; }
            .ctrl-btn.play-btn { width: 56px; height: 56px; }
            .ctrl-btn svg { width: 20px; height: 20px; }
            .ctrl-btn.play-btn svg { width: 24px; height: 24px; }
        }
    </style>
</head>
<body>
<div class="container" id="playerContainer">
    <div class="artwork-container" id="artworkContainer">
        <div class="artwork-placeholder" id="artworkPlaceholder"></div>
        <img class="artwork-image" id="artworkImage" src="" alt="Обложка видео" style="display:none;" onload="onArtworkLoad()" onerror="onArtworkError()">
        <div class="artwork-overlay">
            <div class="play-icon-overlay">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
            </div>
        </div>
    </div>
    <div class="track-info">
        <div class="track-title-row">
            <div class="playing-indicator" id="playingIndicator">
                <div class="eq-bar"></div>
                <div class="eq-bar"></div>
                <div class="eq-bar"></div>
                <div class="eq-bar"></div>
            </div>
            <span class="track-title" id="trackTitle"><span class="track-title-inner">Загрузка...</span></span>
        </div>
        <span class="stream-info" id="streamInfo"><span class="stream-info-inner">&mdash;</span></span>
    </div>
    <div class="track-meta">
        <span class="track-author" id="trackAuthor"></span>
        <span class="track-integrity" id="trackIntegrity" title=""></span>
    </div>
    <div class="progress-section">
        <div class="progress-container" id="progressContainer">
            <div class="progress-bg"></div>
            <div class="progress-fill" id="progressFill"></div>
            <div class="progress-thumb" id="progressThumb"></div>
            <div class="progress-hover-time" id="hoverTime">0:00</div>
        </div>
        <div class="time-info">
            <span id="timeCurrent">0:00</span>
            <span id="timeDuration">0:00</span>
        </div>
    </div>
    <div class="controls">
        <button class="ctrl-btn" id="shuffleBtn" title="Перемешать">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 3 21 3 21 8"></polyline><line x1="4" y1="20" x2="21" y2="3"></line><polyline points="21 16 21 21 16 21"></polyline><line x1="15" y1="15" x2="21" y2="21"></line><line x1="4" y1="4" x2="9" y2="9"></line></svg>
        </button>
        <button class="ctrl-btn" id="prevBtn" title="Назад">
            <svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h2v12H6zm3.5 6l8.5 6V6z"/></svg>
        </button>
        <button class="ctrl-btn play-btn" id="playPauseBtn" title="Воспроизвести">
            <svg id="playIcon" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
            <svg id="pauseIcon" viewBox="0 0 24 24" fill="currentColor" style="display:none;"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
        </button>
        <button class="ctrl-btn" id="nextBtn" title="Вперёд">
            <svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z"/></svg>
        </button>
        <button class="ctrl-btn" id="repeatBtn" title="Повтор">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="17 1 21 5 17 9"></polyline><path d="M3 11V9a4 4 0 0 1 4-4h14"></path><polyline points="7 23 3 19 7 15"></polyline><path d="M21 13v2a4 4 0 0 1-4 4H3"></path></svg>
        </button>
    </div>
    <div class="volume-section">
        <div class="volume-icon" id="volumeIcon" title="Выключить звук">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07" id="volumeWaves"></path></svg>
        </div>
        <input type="range" id="volumeSlider" min="0" max="100" value="100" title="Громкость">
    </div>
    <div class="playlist-section" id="playlistSection">
        <div class="playlist-header" id="playlistHeader">
            <span>Треклист стрима</span>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"></polyline></svg>
        </div>
        <div class="track-list" id="trackList"></div>
    </div>
    <div class="nav-link">
        <a href="index.html">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
            К архиву треклистов
        </a>
    </div>
</div>
<div id="ytplayer"
     style="
        position:absolute;
        width:1px;
        height:1px;
        opacity:0.01;
        pointer-events:none;
     ">
</div>
<script src="https://www.youtube.com/iframe_api"></script>
<script>
var artworkContainer = document.getElementById('artworkContainer');
var artworkImage = document.getElementById('artworkImage');
var artworkPlaceholder = document.getElementById('artworkPlaceholder');
var trackTitle = document.getElementById('trackTitle');
var streamInfo = document.getElementById('streamInfo');
var trackAuthor = document.getElementById('trackAuthor');
var trackIntegrity = document.getElementById('trackIntegrity');
var playingIndicator = document.getElementById('playingIndicator');
var progressContainer = document.getElementById('progressContainer');
var progressFill = document.getElementById('progressFill');
var progressThumb = document.getElementById('progressThumb');
var hoverTime = document.getElementById('hoverTime');
var timeCurrent = document.getElementById('timeCurrent');
var timeDuration = document.getElementById('timeDuration');
var playPauseBtn = document.getElementById('playPauseBtn');
var playIcon = document.getElementById('playIcon');
var pauseIcon = document.getElementById('pauseIcon');
var prevBtn = document.getElementById('prevBtn');
var nextBtn = document.getElementById('nextBtn');
var shuffleBtn = document.getElementById('shuffleBtn');
var repeatBtn = document.getElementById('repeatBtn');
var volumeSlider = document.getElementById('volumeSlider');
var volumeIcon = document.getElementById('volumeIcon');
var volumeWaves = document.getElementById('volumeWaves');
var playlistSection = document.getElementById('playlistSection');
var playlistHeader = document.getElementById('playlistHeader');
var trackList = document.getElementById('trackList');

var masterTrackList = [];
var shuffledList = [];
var currentList = [];
var isShuffled = false;
var isRepeating = false;
var currentTrack = null;
var player = null;
var playerReady = false;
var isPlaying = false;
var checkInterval = null;
var videoDuration = 0;
var volume = 100;
var lastArtworkUrl = '';
var switchingTrack = false;
var pendingAutoplay = false;
var wasMuted = false;
var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
var isChromeIOS = /CriOS/.test(navigator.userAgent);

function parseTimecodeRange(line) {
    var re = /(\d{1,2}:\d{2}(?::\d{2})?)/g;
    var matches = [];
    var m;
    while ((m = re.exec(line)) !== null) { matches.push(m); }
    if (matches.length === 0) return { start: null, end: null, title: line.trim() };
    var startStr = matches[0][1];
    var startSec = getSeconds(startStr);
    if (matches.length >= 2) {
        var between = line.substring(matches[0].index + matches[0][1].length, matches[1].index);
        if (/[-–—]/.test(between)) {
            var endSec = getSeconds(matches[1][1]);
            if (endSec > startSec) {
                var title1 = (line.substring(0, matches[0].index) + line.substring(matches[1].index + matches[1][1].length)).trim();
                return { start: startSec, end: endSec, title: title1.replace(/^[-–—]\s*/, '') };
            }
        }
    }
    var title2 = (line.substring(0, matches[0].index) + line.substring(matches[0].index + matches[0][1].length)).trim();
    return { start: startSec, end: null, title: title2 };
}

function getSeconds(tc) {
    var parts = tc.split(':').map(Number);
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    return 0;
}

function formatTime(seconds) {
    if (isNaN(seconds) || seconds < 0) seconds = 0;
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = Math.floor(seconds % 60);
    if (h > 0) return h + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
    return m + ':' + String(s).padStart(2, '0');
}

function formatTimecode(secs) {
    var h = Math.floor(secs / 3600);
    var m = Math.floor((secs % 3600) / 60);
    var s = secs % 60;
    return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

function extractColorFromImage(img) {
    try {
        var canvas = document.createElement('canvas');
        var ctx = canvas.getContext('2d');
        canvas.width = 1; canvas.height = 1;
        ctx.drawImage(img, 0, 0, 1, 1);
        var data = ctx.getImageData(0, 0, 1, 1).data;
        return { r: data[0], g: data[1], b: data[2] };
    } catch (e) { return { r: 99, g: 102, b: 241 }; }
}

function applyDynamicColor(r, g, b) {
    var hsl = rgbToHsl(r, g, b);
    var primary = 'hsl(' + hsl.h + ', ' + Math.min(hsl.s * 1.3, 100) + '%, ' + Math.max(45, Math.min(65, hsl.l * 1.1)) + '%)';
    document.documentElement.style.setProperty('--primary', primary);
}

function rgbToHsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    var max = Math.max(r, g, b), min = Math.min(r, g, b);
    var h, s, l = (max + min) / 2;
    if (max === min) { h = s = 0; }
    else {
        var d = max - min;
        s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
        switch (max) {
            case r: h = ((g - b) / d + (g < b ? 6 : 0)) / 6; break;
            case g: h = ((b - r) / d + 2) / 6; break;
            case b: h = ((r - g) / d + 4) / 6; break;
        }
    }
    return { h: Math.round(h * 360), s: Math.round(s * 100), l: Math.round(l * 100) };
}

function setupMarquee(element, text) {
    element.classList.remove('marquee');
    var inner = element.querySelector('span');
    inner.textContent = text;
    requestAnimationFrame(function() {
        if (inner.scrollWidth > element.clientWidth) {
            inner.textContent = text + '     ' + text;
            requestAnimationFrame(function() {
                var duration = inner.scrollWidth / 40;
                element.style.setProperty('--marquee-duration', duration + 's');
                element.classList.add('marquee');
            });
        }
    });
}

function forcePlay(retries) {
    if (!player || !playerReady) return;
    var state = player.getPlayerState();
    if (state === YT.PlayerState.PLAYING) return;
    player.playVideo();
    if (retries > 0) {
        setTimeout(function() {
            forcePlay(retries - 1);
        }, 500);
    }
}

function init() {
    fetch('parsed_streams_db.json')
        .then(function(resp) { return resp.json(); })
        .then(function(db) {
            var items = Array.isArray(db) ? db : Object.values(db);
            masterTrackList = [];
            for (var i = 0; i < items.length; i++) {
                var item = items[i];
                if (!item.timecodes || (item.list_type !== 'tracklist' && item.list_type !== 'mixed')) continue;
                for (var j = 0; j < item.timecodes.length; j++) {
                    var parsed = parseTimecodeRange(item.timecodes[j]);
                    if (parsed.start == null) continue;
                    masterTrackList.push({
                        videoId: item.id, start: parsed.start,
                        end: parsed.end != null ? parsed.end : parsed.start + 240,
                        title: parsed.title,
                        streamTitle: item.title || 'Без названия',
                        streamDate: item.date || '',
                        streamAuthor: item.author || '',
                        thumbnail: 'https://img.youtube.com/vi/' + item.id + '/hqdefault.jpg',
                        url: 'https://www.youtube.com/watch?v=' + item.id + '&t=' + parsed.start + 's',
                        videoDuration: item.duration || 0,
                        hasEnd: parsed.end !== null
                    });
                }
            }
            currentList = masterTrackList.slice();
            updateShuffleButton();
            if (currentList.length > 0) {
                if (playerReady) playTrackAtIndex(0);
                else window._playOnReady = function() { playTrackAtIndex(0); };
            }
        })
        .catch(function(e) { console.error(e); });
}

window.onArtworkLoad = function() {
    artworkImage.style.display = 'block';
    artworkPlaceholder.style.display = 'none';
    if (artworkImage.src && artworkImage.src !== lastArtworkUrl) {
        lastArtworkUrl = artworkImage.src;
        try {
            var color = extractColorFromImage(artworkImage);
            applyDynamicColor(color.r, color.g, color.b);
            document.getElementById('playerContainer').style.setProperty('--bg-image', 'url(' + artworkImage.src + ')');
        } catch(e) {}
    }
};

window.onArtworkError = function() {
    artworkImage.style.display = 'none';
    artworkPlaceholder.style.display = 'block';
};

window.onYouTubeIframeAPIReady = function() {
    player = new YT.Player('ytplayer', {
        height: '150',
        width: '200',
        playerVars: {
            autoplay: 1,
            playsinline: 1,
            mute: 0,
            rel: 0
        },
        events: {
            onReady: onPlayerReady,
            onStateChange: onPlayerStateChange
        }
    });
};

function onPlayerReady(event) {
    playerReady = true;
    applyVolumeToPlayer();
    if (window._playOnReady) { window._playOnReady(); window._playOnReady = null; }
}

function onPlayerStateChange(event) {
    var state = event.data;
    
    if ((state === YT.PlayerState.CUED || state === YT.PlayerState.BUFFERING) && pendingAutoplay) {
        pendingAutoplay = false;
        
        if (isChromeIOS) {
            player.mute();
            wasMuted = true;
        }
        
        setTimeout(function() {
            player.playVideo();
            if (wasMuted) {
                setTimeout(function() {
                    player.unMute();
                    wasMuted = false;
                }, 1500);
            }
        }, 500);
        return;
    }
    
    if (state === YT.PlayerState.PLAYING) {
        pendingAutoplay = false;
        isPlaying = true;
        playIcon.style.display = 'none';
        pauseIcon.style.display = '';
        startTimeCheck();
        artworkContainer.classList.add('playing');
        playingIndicator.classList.add('active');
    } else if (state === YT.PlayerState.PAUSED) {
        if (pendingAutoplay && isChromeIOS) {
            forcePlay(5);
            return;
        }
        isPlaying = false;
        playIcon.style.display = '';
        pauseIcon.style.display = 'none';
        if (checkInterval) clearInterval(checkInterval);
        artworkContainer.classList.remove('playing');
        playingIndicator.classList.remove('active');
    } else if (state === YT.PlayerState.ENDED) {
        if (!switchingTrack) {
            if (isRepeating) {
                if (currentTrack && player && playerReady) {
                    player.seekTo(currentTrack.start);
                    setTimeout(function() {
                        player.playVideo();
                    }, 300);
                }
            } else {
                nextTrack();
            }
        }
    }
}

function playTrackAtIndex(index) {
    if (!currentList.length || !player || !playerReady) return;
    var newTrack = currentList[index];
    
    if (currentTrack && currentTrack.videoId === newTrack.videoId) {
        currentTrack = newTrack;
        updateTrackUI();
        player.seekTo(currentTrack.start, true);
        if (isPlaying || isChromeIOS) {
            setTimeout(function() {
                player.playVideo();
            }, 200);
        }
        updateTrackListUI();
        scrollToActiveTrack();
        return;
    }
    
    currentTrack = newTrack;
    updateTrackUI();
    pendingAutoplay = true;
    
    player.loadVideoById({
        videoId: currentTrack.videoId,
        startSeconds: currentTrack.start
    });
    
    updateTrackListUI();
    scrollToActiveTrack();
}

function updateTrackUI() {
    setupMarquee(trackTitle, currentTrack.title);
    setupMarquee(streamInfo, currentTrack.streamTitle + ' \u00b7 ' + currentTrack.streamDate);
    trackAuthor.textContent = currentTrack.streamAuthor ? 'Автор треклиста: ' + currentTrack.streamAuthor : '';
    trackIntegrity.textContent = currentTrack.hasEnd ? '\u2705' : '\u26a0\ufe0f';
    trackIntegrity.title = currentTrack.hasEnd ? 'Треклист имеет начало и конец песни' : 'Треклист не имеет времени конца песни';
    artworkImage.src = currentTrack.thumbnail;
    videoDuration = currentTrack.videoDuration || 0;
    timeDuration.textContent = videoDuration > 0 ? formatTime(videoDuration) : '?:??';
    timeCurrent.textContent = '0:00';
    progressFill.style.width = '0%';
    progressThumb.style.left = '0%';
}

function startTimeCheck() {
    if (checkInterval) clearInterval(checkInterval);
    checkInterval = setInterval(function() {
        if (!player || !player.getCurrentTime || !currentTrack) return;
        if (!videoDuration || videoDuration <= 0) {
            var dur = player.getDuration();
            if (dur && dur > 0) { videoDuration = dur; timeDuration.textContent = formatTime(videoDuration); }
        }
        var currentTime = player.getCurrentTime();
        timeCurrent.textContent = formatTime(currentTime);
        if (videoDuration > 0) {
            var percent = Math.min(100, Math.max(0, (currentTime / videoDuration) * 100));
            progressFill.style.width = percent + '%';
            progressThumb.style.left = percent + '%';
        }
        if (currentTime >= currentTrack.end && !switchingTrack) {
            if (isRepeating) {
                player.seekTo(currentTrack.start);
                setTimeout(function() {
                    player.playVideo();
                }, 300);
            } else {
                nextTrack();
            }
        }
    }, 300);
}

function togglePlayPause() {
    if (!player || !playerReady) return;
    isPlaying ? player.pauseVideo() : player.playVideo();
}

function nextTrack() {
    if (switchingTrack || !currentList.length || !currentTrack) return;
    switchingTrack = true;
    var idx = -1;
    for (var i = 0; i < currentList.length; i++) {
        if (currentList[i].videoId === currentTrack.videoId && currentList[i].start === currentTrack.start) { idx = i; break; }
    }
    if (idx === -1) { switchingTrack = false; return; }
    playTrackAtIndex((idx + 1) % currentList.length);
    setTimeout(function() { switchingTrack = false; }, 1000);
}

function prevTrack() {
    if (switchingTrack || !currentList.length || !currentTrack) return;
    switchingTrack = true;
    var idx = -1;
    for (var i = 0; i < currentList.length; i++) {
        if (currentList[i].videoId === currentTrack.videoId && currentList[i].start === currentTrack.start) { idx = i; break; }
    }
    if (idx === -1) { switchingTrack = false; return; }
    playTrackAtIndex((idx - 1 + currentList.length) % currentList.length);
    setTimeout(function() { switchingTrack = false; }, 1000);
}

function shuffleTracks() {
    if (isShuffled) {
        var idx = -1;
        for (var i = 0; i < masterTrackList.length; i++) {
            if (currentTrack && masterTrackList[i].videoId === currentTrack.videoId && masterTrackList[i].start === currentTrack.start) { idx = i; break; }
        }
        currentList = masterTrackList.slice();
        isShuffled = false;
        if (idx !== -1) playTrackAtIndex(idx);
    } else {
        shuffledList = masterTrackList.slice();
        for (var i = shuffledList.length - 1; i > 0; i--) {
            var j = Math.floor(Math.random() * (i + 1));
            var temp = shuffledList[i]; shuffledList[i] = shuffledList[j]; shuffledList[j] = temp;
        }
        currentList = shuffledList.slice();
        isShuffled = true;
        playTrackAtIndex(0);
    }
    updateShuffleButton();
}

function updateShuffleButton() {
    if (isShuffled) { shuffleBtn.classList.add('shuffle-active'); }
    else { shuffleBtn.classList.remove('shuffle-active'); }
}

function toggleRepeat() {
    isRepeating = !isRepeating;
    if (isRepeating) { repeatBtn.classList.add('repeat-active'); }
    else { repeatBtn.classList.remove('repeat-active'); }
}

function applyVolumeToPlayer() {
    if (player && playerReady) {
        player.setVolume(volume);
        volume === 0 ? player.mute() : player.unMute();
    }
    updateVolumeIcon();
}

function updateVolumeIcon() {
    if (volume === 0) {
        volumeWaves.setAttribute('d', '');
    } else if (volume < 50) {
        volumeWaves.setAttribute('d', 'M15.54 8.46a5 5 0 0 1 0 7.07');
    } else {
        volumeWaves.setAttribute('d', 'M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07');
    }
}

volumeSlider.addEventListener('input', function() {
    volume = parseInt(this.value);
    applyVolumeToPlayer();
});

volumeIcon.addEventListener('click', function() {
    volume = volume > 0 ? 0 : 100;
    volumeSlider.value = volume;
    applyVolumeToPlayer();
});

progressContainer.addEventListener('mousemove', function(e) {
    if (videoDuration <= 0) return;
    var rect = progressContainer.getBoundingClientRect();
    var percent = Math.min(100, Math.max(0, ((e.clientX - rect.left) / rect.width) * 100));
    hoverTime.textContent = formatTime((percent / 100) * videoDuration);
    hoverTime.style.left = percent + '%';
    hoverTime.classList.add('active');
});

progressContainer.addEventListener('mouseleave', function() { hoverTime.classList.remove('active'); });

progressContainer.addEventListener('click', function(e) {
    if (!player || !playerReady || videoDuration <= 0) return;
    var rect = progressContainer.getBoundingClientRect();
    var percent = Math.min(100, Math.max(0, ((e.clientX - rect.left) / rect.width) * 100));
    player.seekTo((percent / 100) * videoDuration, true);
});

function updateTrackListUI() {
    if (!currentTrack) return;
    var streamTracks = [];
    for (var i = 0; i < masterTrackList.length; i++) {
        if (masterTrackList[i].videoId === currentTrack.videoId) streamTracks.push(masterTrackList[i]);
    }
    var html = '';
    for (var i = 0; i < streamTracks.length; i++) {
        var tr = streamTracks[i];
        var isActive = tr.start === currentTrack.start;
        html += '<div class="track-item' + (isActive ? ' active' : '') + '" data-videoid="' + tr.videoId + '" data-start="' + tr.start + '">';
        html += '<span class="tc-time">' + formatTimecode(tr.start) + '</span>';
        html += '<span class="tc-title">' + tr.title.replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</span>';
        if (tr.hasEnd) html += '<span class="tc-duration">' + formatTime(tr.end - tr.start) + '</span>';
        html += '</div>';
    }
    trackList.innerHTML = html;
    playlistHeader.querySelector('span').textContent = 'Треклист (' + streamTracks.length + ')';
}

function scrollToActiveTrack() {
    setTimeout(function() {
        var activeItem = trackList.querySelector('.track-item.active');
        if (activeItem) activeItem.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }, 200);
}

playPauseBtn.addEventListener('click', togglePlayPause);
nextBtn.addEventListener('click', nextTrack);
prevBtn.addEventListener('click', prevTrack);
shuffleBtn.addEventListener('click', shuffleTracks);
repeatBtn.addEventListener('click', toggleRepeat);

artworkContainer.addEventListener('click', function() {
    if (currentTrack) window.open(currentTrack.url, '_blank');
});

playlistHeader.addEventListener('click', function() {
    playlistSection.classList.toggle('open');
    if (playlistSection.classList.contains('open') && currentTrack) scrollToActiveTrack();
});

trackList.addEventListener('click', function(e) {
    var trackItem = e.target.closest('.track-item');
    if (!trackItem) return;
    var videoId = trackItem.getAttribute('data-videoid');
    var start = parseInt(trackItem.getAttribute('data-start'));
    for (var i = 0; i < currentList.length; i++) {
        if (currentList[i].videoId === videoId && currentList[i].start === start) {
            playTrackAtIndex(i); return;
        }
    }
    for (var i = 0; i < masterTrackList.length; i++) {
        if (masterTrackList[i].videoId === videoId && masterTrackList[i].start === start) {
            isShuffled = false; updateShuffleButton();
            currentList = masterTrackList.slice();
            playTrackAtIndex(i); return;
        }
    }
});

document.addEventListener('keydown', function(e) {
    if (e.target.tagName === 'INPUT') return;
    if (e.code === 'Space') { e.preventDefault(); togglePlayPause(); }
    else if (e.code === 'ArrowLeft' && player && playerReady) { e.preventDefault(); player.seekTo(Math.max(currentTrack ? currentTrack.start : 0, player.getCurrentTime() - 5), true); }
    else if (e.code === 'ArrowRight' && player && playerReady) { e.preventDefault(); player.seekTo(player.getCurrentTime() + 5, true); }
});

if (typeof YT !== 'undefined' && YT.Player) window.onYouTubeIframeAPIReady();
window.addEventListener('load', init);
</script>
</body>
</html>'''

    with open(player_html_path, 'w', encoding='utf-8') as f:
        f.write(player_html)
    logging.info("player.html создан: %s", os.path.abspath(player_html_path))

    # ---------- Основной index.html ----------
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
