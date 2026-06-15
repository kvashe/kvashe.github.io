import os
import re
import json
import threading
import textwrap
import requests
import sys
from datetime import datetime
from yt_dlp import YoutubeDL
from concurrent.futures import ThreadPoolExecutor

# ==================== НАСТРОЙКИ ====================
API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
if not API_KEY:
    raise ValueError("Переменная окружения YOUTUBE_API_KEY не установлена")

CHANNEL_STREAMS_URL = 'https://www.youtube.com/@kvashenaya/streams'
DB_FILE = "parsed_streams_db.json"
OUTPUT_HTML = "index.html"

NEW_STREAMS_TO_CHECK = 999
MAX_WORKERS = 4
MIN_TIMECODES_COUNT = 5

MIN_WORDS_AFTER_TIMECODE = 2
MAX_WORDS_AFTER_TIMECODE = 30
MIN_AVG_TIMECODE_GAP = 20

FORCE_AUTHOR = None

FORBIDDEN_PHRASES = [
    "хватит брать высокие ноты",
    "пой без кривляний",
    "но вот играть на ней ты явно неумеешь",
]
# ===================================================

db_lock = threading.Lock()


def load_database():
    if not os.path.exists(DB_FILE):
        return {}
    db = {}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {}
            try:
                data = json.loads(content)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "id" in item:
                            db[item["id"]] = item
                    return db
            except json.JSONDecodeError:
                pass
            f.seek(0)
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                try:
                    obj = json.loads(stripped)
                    video_id = obj.get("id")
                    if video_id:
                        db[video_id] = obj
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"Предупреждение: ошибка чтения базы ({e}). Будет создана новая.")
        return {}
    return db


def save_single_video_to_db(video_id, video_data):
    with db_lock:
        current_db = load_database()
        current_db[video_id] = video_data
        sorted_items = sorted(current_db.values(), key=lambda x: x.get("raw_date", "00000000"), reverse=True)
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted_items, f, ensure_ascii=False, indent=2)


def clean_timecode_range(text):
    time_pattern = r'\b(?:\d{1,2}:)?\d{1,2}:\d{2}\b'
    range_pattern = rf'({time_pattern})\s+[-–—]\s+{time_pattern}'
    return re.sub(range_pattern, r'\1', text)


def count_timecodes(text):
    time_pattern = r'\b(?:\d{1,2}:)?\d{1,2}:\d{2}\b'
    return sum(1 for line in text.split('\n') if re.search(time_pattern, line))


def get_timecode_seconds(line):
    time_pattern = r'\b(?:\d{1,2}:)?\d{1,2}:\d{2}\b'
    match = re.search(time_pattern, line)
    if match:
        time_str = match.group(0)
        parts = [int(p) for p in time_str.split(':')]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        elif len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 99999999


def is_transcript_like(lines):
    times = []
    for line in lines:
        sec = get_timecode_seconds(line)
        if sec != 99999999:
            times.append(sec)
    if len(times) < 5:
        return False
    deltas = []
    for i in range(len(times) - 1):
        diff = times[i + 1] - times[i]
        if diff > 0:
            deltas.append(diff)
    if not deltas:
        return False
    avg_delta = sum(deltas) / len(deltas)
    if avg_delta < MIN_AVG_TIMECODE_GAP:
        return True
    return False


def is_good_timecode_line(line):
    time_pattern = r'\b(?:\d{1,2}:)?\d{1,2}:\d{2}\b'
    match = re.search(time_pattern, line)
    if not match:
        return False
    after_time = line[match.end():].strip()
    if not after_time:
        return False
    clean_after = re.sub(r'[^\w\sа-яА-ЯёЁA-Za-z]', ' ', after_time)
    words = clean_after.split()
    word_count = len(words)

    if ' - ' in after_time:
        max_words = 50
    else:
        max_words = MAX_WORDS_AFTER_TIMECODE

    special_allowed = [
        "титры", "интро", "начало", "конец", "финал",
        "донаты", "розыгрыш", "чат", "сигн",
        "вступление", "intro", "outro", "припев", "куплет"
    ]
    lower_after = after_time.lower()
    if word_count < MIN_WORDS_AFTER_TIMECODE:
        allowed = any(word in lower_after for word in special_allowed)
        if not allowed:
            return False
    if word_count > max_words:
        return False
    banned_words = ["привет", "ага", "сегодня", "ладно", "понятно", "ок", "ну", "блин"]
    if lower_after in banned_words:
        return False
    return True


def is_clean_tracklist(comment_text):
    time_pattern = r'\b(?:\d{1,2}:)?\d{1,2}:\d{2}\b'
    valid_lines = []
    for line in comment_text.split('\n'):
        if re.search(time_pattern, line):
            line = clean_timecode_range(line.strip())
            if not is_good_timecode_line(line):
                return False
            valid_lines.append(line)
    if is_transcript_like(valid_lines):
        return False
    return True


def extract_smart_timecodes(comments):
    candidates = []
    mixed_lines = []
    mixed_authors = set()

    print(f"    [DEBUG] Всего комментариев с таймкодами для анализа: {len(comments) if comments else 0}")
    if comments:
        for idx, c in enumerate(comments[:10]):
            print(f"    [DEBUG] Комментарий {idx+1}: автор='{c.get('author', '?')}', текст='{c.get('text', '')[:80]}...'")

    for comment in comments:
        text = comment.get('text', '')
        text_lower = text.lower()
        if any(phrase.lower() in text_lower for phrase in FORBIDDEN_PHRASES):
            print(f"    [DEBUG] Игнорируем комментарий автора {comment.get('author', '?')} (содержит запрещённую фразу)")
            continue

        all_lines = [
            line.strip()
            for line in text.split('\n')
            if re.search(r'\b(?:\d{1,2}:)?\d{1,2}:\d{2}\b', line)
        ]
        valid_lines = [
            clean_timecode_range(line)
            for line in all_lines
            if is_good_timecode_line(line)
        ]
        tc_count = len(valid_lines)

        if tc_count >= MIN_TIMECODES_COUNT:
            if is_transcript_like(valid_lines):
                continue
            music_score = 0
            for line in valid_lines:
                if ' - ' in line:
                    music_score += 3
                if re.search(r'[A-Za-z]', line):
                    music_score += 1
                if '(' in line or ')' in line:
                    music_score += 1
                bad_words = ['стрим', 'волос', 'сигна', 'говорит', 'чат', 'вопрос',
                             'talking', 'спросить', 'умница', 'красив']
                lower = line.lower()
                if any(w in lower for w in bad_words):
                    music_score -= 2

            author = comment.get('author', 'Неизвестно')
            if author in ("@ajoajo701", "@mirovoy100"):
                music_score = -1_000_000
                print(f"    [DEBUG] Автор {author} получает штраф")

            candidates.append({
                'text': text,
                'valid_lines': valid_lines,
                'tc_count': tc_count,
                'music_score': music_score,
                'author': author,
            })
        elif tc_count > 0:
            mixed_lines.extend(valid_lines)
            mixed_authors.add(comment.get('author', 'Неизвестно'))

    if candidates:
        best = max(candidates, key=lambda c: (c['music_score'], c['tc_count']))
        print(f"    [DEBUG] Выбран кандидат от автора: {best['author']}")
        return (
            sorted(list(set(best['valid_lines'])), key=get_timecode_seconds),
            "tracklist",
            best['author']
        )
    if mixed_lines:
        unique_lines = sorted(list(set(mixed_lines)), key=get_timecode_seconds)
        authors_str = ", ".join(list(mixed_authors)[:3])
        if len(mixed_authors) > 3:
            authors_str += " и др."
        print(f"    [DEBUG] Смешанный режим, авторы: {authors_str}")
        return unique_lines, "mixed", authors_str
    return [], "none", ""


def get_video_comments_via_api(video_id, max_comments=3000):
    comments = []
    page_token = None

    while len(comments) < max_comments:
        params = {
            "key": API_KEY,
            "part": "snippet",
            "videoId": video_id,
            "maxResults": 100,
            "textFormat": "plainText",
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            resp = requests.get("https://www.googleapis.com/youtube/v3/commentThreads", params=params)
            data = resp.json()
        except Exception as e:
            print(f"    [ОШИБКА API] Не удалось загрузить комментарии для {video_id}: {e}")
            break

        if "error" in data:
            print(f"    [ОШИБКА API] {data['error'].get('message', 'Неизвестная ошибка')}")
            break

        for item in data.get("items", []):
            snippet = item["snippet"]["topLevelComment"]["snippet"]
            comments.append({
                "text": snippet.get("textDisplay", ""),
                "author": snippet.get("authorDisplayName", ""),
                "is_pinned": False,
            })
            if len(comments) >= max_comments:
                break

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return comments


def parse_single_video(video_entry):
    video_id = video_entry.get('id')
    title = video_entry.get('title', 'No Title')
    print(f"    [СТАРТ ПАРСИНГА] -> {title[:30]}...")

    comments = get_video_comments_via_api(video_id)

    if comments:
        first_text = comments[0].get('text', '')[:150]
        print(f"    [ДИАГНОСТИКА] Первый комментарий: {first_text}...")
    else:
        print("    [ДИАГНОСТИКА] Комментариев не найдено")

    timecodes, list_type, author = extract_smart_timecodes(comments)

    if FORCE_AUTHOR is not None:
        author = FORCE_AUTHOR
        print(f"    [AUTHOR] Принудительно установлен автор: {author}")

    raw_date = "00000000"
    try:
        video_resp = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"key": API_KEY, "part": "snippet", "id": video_id}
        ).json()
        if "items" in video_resp and video_resp["items"]:
            published_at = video_resp["items"][0]["snippet"]["publishedAt"]
            raw_date = published_at[:10].replace("-", "")
    except Exception as e:
        print(f"    [WARN] Не удалось получить дату для {video_id}: {e}")

    formatted_date = f"{raw_date[6:8]}.{raw_date[4:6]}.{raw_date[0:4]}" if len(raw_date) == 8 else "Неизвестно"

    video_data = {
        "id": video_id, "title": title, "date": formatted_date,
        "raw_date": raw_date, "timecodes": timecodes,
        "list_type": list_type, "author": author
    }
    save_single_video_to_db(video_id, video_data)
    print(f"    [СОХРАНЕНО] -> {title[:25]}... ({formatted_date}) автор={author}")
    return True


def generate_sitemap(streams):
    sitemap = '<?xml version="1.0" encoding="UTF-8"?>\n'
    sitemap += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    sitemap += f'''
    <url>
        <loc>https://kvash9.github.io/arh/</loc>
        <lastmod>{datetime.now().date().isoformat()}</lastmod>
        <priority>1.00</priority>
    </url>\n'''
    for stream in streams:
        video_url = f"https://www.youtube.com/watch?v={stream['id']}"
        lastmod = stream.get('raw_date', datetime.now().date().isoformat())
        if len(lastmod) == 8:
            lastmod = f"{lastmod[:4]}-{lastmod[4:6]}-{lastmod[6:8]}"
        sitemap += f'''
    <url>
        <loc>{video_url}</loc>
        <lastmod>{lastmod}</lastmod>
        <priority>0.80</priority>
    </url>\n'''
    sitemap += '</urlset>'
    with open("sitemap.xml", "w", encoding="utf-8") as f:
        f.write(sitemap)
    print("sitemap.xml сгенерирован")


def generate_robots_txt():
    robots = """User-agent: *
Allow: /
Sitemap: https://kvash9.github.io/arh/sitemap.xml
"""
    with open("robots.txt", "w", encoding="utf-8") as f:
        f.write(robots)
    print("robots.txt сгенерирован")


def generate_html_report():
    print("Генерация статического index.html...")
    db_data = load_database()
    sorted_streams = sorted(db_data.values(), key=lambda x: x.get("raw_date", "00000000"), reverse=True)

    preloaded_json = json.dumps(sorted_streams, ensure_ascii=False)

    # HTML-шаблон с исправленными регулярками
    html_template = r"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Архив трансляций Квашеной — треклисты и таймкоды</title>
    <meta name="description" content="Архив YouTube-стримов Квашеной с готовыми треклистами и таймкодами. Поиск песен по удобному каталогу.">
    <meta name="keywords" content="Квашеная, стримы, треклисты, песни, YouTube, архив">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://kvash9.github.io/arh/">
    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🎵</text></svg>">
    <style>
        /* Стили (сокращены для краткости, но в реальном коде они полные) */
        :root { --primary: #6366f1; --primary-hover: #4f46e5; --bg: #0b0f19; --card-bg: rgba(22,28,45,0.6); --text-main: #f1f5f9; --text-muted: #94a3b8; --border: rgba(255,255,255,0.08); }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0 0 60px; color: var(--text-main); background: #0b1020; min-height: 100vh; overflow-x: hidden; }
        .scroll-top { position: fixed; bottom: 30px; right: 30px; width: 48px; height: 48px; background: rgba(23,29,61,0.28); backdrop-filter: blur(8px); border: 1px solid rgba(255,255,255,0.2); border-radius: 50%; color: white; font-size: 24px; cursor: pointer; display: flex; align-items: center; justify-content: center; opacity: 0; visibility: hidden; transition: all 0.3s ease; z-index: 1000; }
        .scroll-top.show { opacity: 1; visibility: visible; }
        .skeleton-row { background: linear-gradient(180deg, rgba(30,38,58,0.8), rgba(22,28,45,0.9)); border: 1px solid rgba(255,255,255,0.06); border-radius: 20px; padding: 24px; display: flex; flex-direction: row; gap: 24px; margin-bottom: 24px; position: relative; overflow: hidden; }
        .skeleton-img { width: 160px; height: 90px; background: #1e293b; border-radius: 12px; }
        .skeleton-content { flex: 1; display: flex; flex-direction: column; gap: 16px; }
        .skeleton-title { width: 70%; height: 24px; background: #1e293b; border-radius: 8px; }
        .shimmer { position: relative; overflow: hidden; }
        .shimmer::after { content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: linear-gradient(110deg, rgba(255,255,255,0) 0%, rgba(255,255,255,0.06) 40%, rgba(255,255,255,0) 60%); animation: shimmerMove 1.2s infinite linear; }
        @keyframes shimmerMove { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }
        .parallax-notes { position: fixed; top: 0; left: 0; width: 100%; height: 100%; overflow: hidden; pointer-events: none; z-index: 0; }
        .note { position: absolute; user-select: none; pointer-events: none; }
        .note-content { display: inline-block; animation: gentleFloat 6s infinite ease-in-out; }
        @keyframes gentleFloat { 0% { transform: translateY(0px); } 50% { transform: translateY(-12px); } 100% { transform: translateY(0px); } }
        .container { max-width: 1000px; margin: 0 auto; padding: 0 24px; }
        .header-panel { position: sticky; top: 0; background: rgba(11,15,25,0.75); border-bottom: 1px solid var(--border); z-index: 100; padding: 20px 0 12px; margin-bottom: 40px; }
        .header-flex { display: flex; align-items: center; justify-content: space-between; gap: 20px; flex-wrap: wrap; }
        h2 { color: #fff; font-size: 24px; font-weight: 800; margin: 0; display: flex; align-items: center; gap: 12px; }
        .search-box { flex-grow: 1; max-width: 400px; }
        .s-input { width: 100%; padding: 12px 44px 12px 18px; font-size: 15px; border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; background: rgba(15,23,42,0.6); color: #fff; }
        .s-input:focus { border-color: var(--primary); outline: none; }
        #searchStats { color: var(--text-muted); font-size: 13px; margin-top: 8px; }
        .year-filters { display: flex; gap: 10px; margin-bottom: 30px; overflow-x: auto; }
        .year-btn { background: rgba(30,41,59,0.5); border: 1px solid rgba(255,255,255,0.06); padding: 10px 20px; border-radius: 10px; color: var(--text-muted); cursor: pointer; }
        .year-btn.active { background: var(--primary); color: #fff; }
        .grid { display: flex; flex-direction: column; gap: 24px; }
        .row { position: relative; display: flex; flex-direction: row; gap: 24px; padding: 24px; background: linear-gradient(180deg, rgba(20,28,48,0.88), rgba(15,22,40,0.94)); border: 1px solid rgba(255,255,255,0.06); border-radius: 20px; }
        .v-date { position: absolute; top: 24px; right: 24px; background: rgba(15,23,42,0.6); padding: 6px 12px; border-radius: 8px; font-size: 13px; }
        .img-container { width: 160px; height: 90px; border-radius: 12px; overflow: hidden; background: #151c2d; }
        .img-container img { width: 100%; height: 100%; object-fit: cover; }
        .v-title { font-size: 18px; color: #f8fafc; font-weight: 700; }
        details { background: rgba(15,23,42,0.4); padding: 12px 18px; border-radius: 12px; margin-top: 16px; }
        summary { font-weight: 600; cursor: pointer; }
        .summary-flex { display: flex; justify-content: space-between; }
        .tc-list { margin-top: 16px; max-height: 280px; overflow-y: auto; }
        .tc-item { margin-bottom: 10px; display: flex; align-items: center; gap: 12px; }
        .t-click { background: rgba(99,102,241,0.15); color: #a5b4fc; padding: 3px 10px; border-radius: 6px; cursor: pointer; }
        .badge-tracklist { background: rgba(16,185,129,0.1); color: #34d399; padding: 5px 12px; border-radius: 8px; font-size: 12px; }
        .badge-mixed { background: rgba(245,158,11,0.1); color: #fbbf24; padding: 5px 12px; border-radius: 8px; font-size: 12px; }
        mark { background: rgba(139,92,246,0.35); color: white; padding: 1px; border-radius: 6px; }
        @media (max-width: 768px) { .row { flex-direction: column; } .v-date { position: static; margin-bottom: 8px; } .img-container { width: 100%; height: auto; aspect-ratio: 16/9; } .v-content-block { padding-right: 0; } }
    </style>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "CollectionPage",
      "name": "Архив трансляций Квашеной",
      "description": "Архив YouTube-стримов Квашеной с готовыми треклистами и таймкодами.",
      "url": "https://kvash9.github.io/arh/",
      "numberOfItems": {len(sorted_streams)}
    }
    </script>
</head>
<body>
<div class="parallax-notes" id="parallaxNotes"></div>
<div class="header-panel">
    <div class="container header-flex">
        <h2>Архив трансляций <span style="background:linear-gradient(135deg,#a5b4fc,#6366f1); -webkit-background-clip:text; -webkit-text-fill-color:transparent;">Квашеной</span></h2>
        <div class="search-box">
            <input type="text" id="sInput" class="s-input" placeholder="Поиск песни">
            <div id="searchStats">Найдено трансляций: {len(sorted_streams)}</div>
        </div>
    </div>
</div>
<div class="container">
    <div id="yearFilters" class="year-filters"></div>
    <div class="grid" id="mainGrid">
        <div class="skeleton-row shimmer"><div class="skeleton-img shimmer"></div><div class="skeleton-content"><div class="skeleton-title shimmer"></div></div></div>
        <div class="skeleton-row shimmer"><div class="skeleton-img shimmer"></div><div class="skeleton-content"><div class="skeleton-title shimmer"></div></div></div>
    </div>
</div>
<button class="scroll-top" id="scrollTopBtn">↑</button>
<script>
window.__PRELOADED_DATA__ = {preloaded_json};

function removeSpecificEmojis(str) {
    if (typeof Intl !== 'undefined' && Intl.Segmenter) {
        const segmenter = new Intl.Segmenter('en', { granularity: 'grapheme' });
        const segments = [...segmenter.segment(str)];
        let result = '';
        for (const seg of segments) {
            const grapheme = seg.segment;
            const isEmoji = /\p{Emoji}/u.test(grapheme) && !/[\p{N}\p{L}]/u.test(grapheme);
            if (!isEmoji) {
                result += grapheme;
            }
        }
        return result;
    } else {
        return str.replace(/[\u{1F600}-\u{1F64F}\u{1F300}-\u{1F5FF}\u{1F680}-\u{1F6FF}\u{1F1E0}-\u{1F1FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{1F900}-\u{1F9FF}\u{1FA00}-\u{1FA6F}\u{1FA70}-\u{1FAFF}]/gu, '');
    }
}

function normalize(str) {
    return str.toLowerCase().replace(/ë/g, 'e').replace(/[^a-zа-яё0-9]/g, '');
}

const SYNONYMS = {
    "noizemc": ["noizemc","noizemc","нойзмс","нойзмс","noize","нойз"],
    "rammstein": ["rammstein","раммштайн"],
    "корольишут": ["корольишут","киш"],
    "витас": ["витас","vitas"],
    "ladygaga": ["ladygaga","ледигага"],
    "максим": ["максим","макsим"],
    "fleur": ["fleur","flëur"],
    "nautiluspompilius": ["nautiluspompilius","наутилуспомпилиус","pompilius","nautilus","наутилус","помпилиус"],
    "океанэлзи": ["океанэлзи","элзи","океанэльзы","эльзы","океанельзи","ельзи"]
};
const variantToCanon = new Map();
for (const [canon, variants] of Object.entries(SYNONYMS)) {
    for (const v of variants) variantToCanon.set(v, canon);
}
function getSearchVariants(query) {
    const normQuery = normalize(query);
    if (variantToCanon.has(normQuery)) return SYNONYMS[variantToCanon.get(normQuery)];
    return [normQuery];
}
function matchesWithVariants(textNorm, variants) {
    for (let v of variants) if (textNorm.includes(normalize(v))) return true;
    return false;
}
function highlightFirstMatch(original, variants) {
    if (!variants || variants.length===0) return original;
    const normOriginal = normalize(original);
    let bestMatch = null, bestIndex = Infinity;
    for (let v of variants) {
        const idx = normOriginal.indexOf(normalize(v));
        if (idx !== -1 && idx < bestIndex) { bestIndex = idx; bestMatch = v; }
    }
    if (bestMatch===null) return original;
    let origIdx=0, normIdx=0;
    while (normIdx < bestIndex && origIdx < original.length) {
        const ch = original[origIdx];
        if (normalize(ch).length) normIdx++;
        origIdx++;
    }
    const startOrig = origIdx;
    while (normIdx < bestIndex + bestMatch.length && origIdx < original.length) {
        const ch = original[origIdx];
        if (normalize(ch).length) normIdx++;
        origIdx++;
    }
    const endOrig = origIdx;
    return original.substring(0,startOrig) + '<mark>' + original.substring(startOrig,endOrig) + '</mark>' + original.substring(endOrig);
}

let streamsData = [], songsDB = {}, searchIndex = [], activeYear = 'all', allRows = [];

function getTimecodeSeconds(line) {
    const m = line.match(/(\d{1,2}:\d{2}(?::\d{2})?)/);
    if (!m) return 99999999;
    const parts = m[1].split(':').map(Number);
    if (parts.length===2) return parts[0]*60+parts[1];
    if (parts.length===3) return parts[0]*3600+parts[1]*60+parts[2];
    return 99999999;
}
function normalizeTimecode(tc) {
    const parts = tc.split(':').map(Number);
    if (parts.length===2) return `00:${parts[0].toString().padStart(2,'0')}:${parts[1].toString().padStart(2,'0')}`;
    if (parts.length===3) return parts.map(p=>p.toString().padStart(2,'0')).join(':');
    return tc;
}

async function loadDatabase() {
    if (streamsData.length) return;
    if (window.__PRELOADED_DATA__ && window.__PRELOADED_DATA__.length) {
        streamsData = window.__PRELOADED_DATA__;
    } else {
        try {
            const resp = await fetch('parsed_streams_db.json');
            if (!resp.ok) throw new Error();
            streamsData = await resp.json();
        } catch(e) { console.error(e); streamsData = []; return; }
    }
    songsDB = {}; searchIndex = [];
    streamsData.forEach(entry => {
        const vId = entry.id;
        const timecodes = entry.timecodes || [];
        const sorted = timecodes.slice().sort((a,b)=>getTimecodeSeconds(a)-getTimecodeSeconds(b));
        const tracks = sorted.map(line => {
            const match = line.match(/(\d{1,2}:\d{2}(?::\d{2})?)/);
            if (match) {
                let s = line.replace(match[1],'').trim().replace(/^[-–—]\s*/,'');
                s = removeSpecificEmojis(s);
                return { t: match[1], s: s };
            } else {
                return { s: removeSpecificEmojis(line) };
            }
        });
        songsDB[vId] = { tracks, author: entry.author || '' };
        tracks.forEach(tr => {
            const norm = normalize(tr.s || '');
            searchIndex.push({ id: vId, text: tr.s, norm });
        });
    });
}

function renderStreamHTML(stream) {
    const vId = stream.id, title = stream.title || 'Без названия', date = stream.date || 'Неизвестно';
    const rawYear = (stream.raw_date || '0000').substring(0,4);
    const timecodes = stream.timecodes || [];
    const listType = stream.list_type || 'none';
    const hasTracks = timecodes.length>0;
    const badgeClass = (listType==='tracklist'||listType==='mixed') ? `badge-${listType}` : 'hide';
    const badgeText = listType==='tracklist' ? 'Готовый трек-лист' : (listType==='mixed' ? 'Сборный список' : '');
    const tcsHTML = hasTracks ? `<details><summary><div class="summary-flex"><span>Треклист ${timecodes.length}</span><span class="${badgeClass}">${badgeText}</span></div></summary><div class="tc-list"></div></details>` : '<div class="no-tc-block"><span>Треклист не найден</span></div>';
    return `<div class="row" data-id="${vId}" data-year="${rawYear}" style="--bg-thumb: url('https://img.youtube.com/vi/${vId}/hqdefault.jpg');"><span class="v-date">${date}</span><a class="v-link" href="https://www.youtube.com/watch?v=${vId}" target="_blank"><div class="img-container"><img loading="lazy" decoding="async" src="https://img.youtube.com/vi/${vId}/hqdefault.jpg" alt=""></div></a><div class="v-content-block"><a href="https://www.youtube.com/watch?v=${vId}" target="_blank" style="text-decoration:none"><span class="v-title">${title}</span></a><div class="v-tcs">${tcsHTML}</div></div></div>`;
}

function animateContainer(container) {
    container.style.transition = 'none';
    container.style.opacity = '0';
    container.style.transform = 'translateY(-10px)';
    void container.offsetHeight;
    container.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
    container.style.opacity = '1';
    container.style.transform = 'translateY(0)';
}

function renderAllStreams() {
    const grid = document.getElementById('mainGrid');
    grid.innerHTML = streamsData.map(renderStreamHTML).join('');
    allRows = [...document.querySelectorAll('.row')];
    document.querySelectorAll('details').forEach(details => {
        details.addEventListener('toggle', function() {
            const container = this.querySelector('.tc-list');
            if (!container) return;
            if (this.open) {
                const row = this.closest('.row');
                const vId = row.getAttribute('data-id');
                const filter = document.getElementById('sInput').value.toLowerCase().trim();
                if (container.children.length === 0) renderTracklist(vId, container, filter);
                animateContainer(container);
                if (filter) setTimeout(() => { const mark = container.querySelector('mark'); if(mark) mark.closest('.tc-item')?.scrollIntoView({block:'nearest'}); }, 50);
            } else {
                container.style.transition = 'none';
                container.style.opacity = '0';
                container.style.transform = 'translateY(-10px)';
            }
        });
    });
}

function initYearFilters() {
    const years = new Set();
    allRows.forEach(row => { const y = row.getAttribute('data-year'); if(y && y!=='0000') years.add(y); });
    const sorted = Array.from(years).sort().reverse();
    const container = document.getElementById('yearFilters');
    let html = '<button class="year-btn active" data-year="all">Все годы</button>';
    sorted.forEach(y => html += `<button class="year-btn" data-year="${y}">${y} года</button>`);
    container.innerHTML = html;
    container.addEventListener('click', (e) => {
        if(e.target.classList.contains('year-btn')) {
            document.querySelectorAll('.year-btn').forEach(b => b.classList.remove('active'));
            e.target.classList.add('active');
            activeYear = e.target.getAttribute('data-year');
            executeSearch(document.getElementById('sInput').value.toLowerCase().trim());
        }
    });
}

function renderTracklist(vId, container, filter) {
    const tracks = songsDB[vId]?.tracks || [];
    let html = '';
    if (filter) {
        const variants = getSearchVariants(filter);
        tracks.forEach(tr => {
            let sText = tr.s;
            const normText = normalize(sText);
            if (matchesWithVariants(normText, variants)) sText = highlightFirstMatch(sText, variants);
            const displayedTime = tr.t ? normalizeTimecode(tr.t) : '';
            html += tr.t ? `<div class="tc-item"><span class="t-click" data-time="${tr.t}">${displayedTime}</span><span class="s-title">${sText}</span></div>` : `<div class="tc-item"><span class="s-title">${sText}</span></div>`;
        });
    } else {
        tracks.forEach(tr => {
            const displayedTime = tr.t ? normalizeTimecode(tr.t) : '';
            html += tr.t ? `<div class="tc-item"><span class="t-click" data-time="${tr.t}">${displayedTime}</span><span class="s-title">${tr.s}</span></div>` : `<div class="tc-item"><span class="s-title">${tr.s}</span></div>`;
        });
    }
    if (songsDB[vId]?.author) html += `<div class="tc-author">Автор треклиста: ${songsDB[vId].author}</div>`;
    container.innerHTML = html;
}

const searchInput = document.getElementById('sInput');
const statsEl = document.getElementById('searchStats');

async function executeSearch(filter) {
    if (!allRows.length) await loadDatabase();
    if (!allRows.length) return;
    let visibleCount = 0;
    const variants = filter ? getSearchVariants(filter) : [];
    document.getElementById('sClear') && (document.getElementById('sClear').style.display = filter ? 'flex' : 'none');
    allRows.forEach(row => {
        row.style.display = 'none';
        const details = row.querySelector('details');
        if (details) {
            details.open = false;
            const tcList = details.querySelector('.tc-list');
            if (tcList) { tcList.style.transition = 'none'; tcList.style.opacity = '0'; tcList.style.transform = 'translateY(-10px)'; tcList.innerHTML = ''; }
        }
    });
    if (!filter) {
        allRows.forEach(row => { const rowYear = row.getAttribute('data-year'); if (activeYear === 'all' || rowYear === activeYear) { row.style.display = ''; visibleCount++; } });
        statsEl.textContent = 'Найдено трансляций: ' + visibleCount;
        return;
    }
    const matchedIds = new Set();
    searchIndex.forEach(item => { if (matchesWithVariants(item.norm, variants)) matchedIds.add(item.id); });
    const isDesktop = window.innerWidth > 768;
    allRows.forEach(row => {
        const vId = row.getAttribute('data-id'), rowYear = row.getAttribute('data-year');
        if (activeYear !== 'all' && rowYear !== activeYear) return;
        if (!matchedIds.has(vId)) return;
        row.style.display = '';
        visibleCount++;
        if (isDesktop && filter.length >= 2) {
            const details = row.querySelector('details');
            const tcList = row.querySelector('.tc-list');
            if (details && tcList && tcList.children.length === 0) {
                details.open = true;
                renderTracklist(vId, tcList, filter);
                animateContainer(tcList);
                setTimeout(() => { const mark = tcList.querySelector('mark'); if(mark) mark.closest('.tc-item')?.scrollIntoView({block:'nearest'}); }, 50);
            }
        }
    });
    statsEl.textContent = 'Найдено трансляций: ' + visibleCount;
}

document.getElementById('scrollTopBtn').addEventListener('click', () => window.scrollTo({top:0,behavior:'smooth'}));
window.addEventListener('scroll', () => { document.getElementById('scrollTopBtn').classList.toggle('show', window.scrollY>300); });

document.addEventListener('DOMContentLoaded', async () => {
    await loadDatabase();
    renderAllStreams();
    initYearFilters();
    executeSearch('');
    const clearBtn = document.createElement('button');
    clearBtn.id = 'sClear';
    clearBtn.textContent = '✕';
    clearBtn.style.cssText = 'position:absolute; right:14px; background:rgba(255,255,255,0.1); border:none; width:22px; height:22px; border-radius:50%; color:#94a3b8; cursor:pointer; display:none;';
    document.querySelector('.search-box').style.position = 'relative';
    document.querySelector('.search-box').appendChild(clearBtn);
    clearBtn.onclick = () => { searchInput.value = ''; clearBtn.style.display = 'none'; executeSearch(''); };
    searchInput.addEventListener('input', () => { clearBtn.style.display = searchInput.value ? 'flex' : 'none'; clearTimeout(window.debounceTimer); window.debounceTimer = setTimeout(() => executeSearch(searchInput.value.toLowerCase().trim()), 700); });
});

document.getElementById('mainGrid').addEventListener('click', (e) => {
    if (e.target.classList.contains('t-click')) {
        e.preventDefault();
        const time = e.target.getAttribute('data-time');
        const vId = e.target.closest('.row').getAttribute('data-id');
        const parts = time.split(':').map(Number);
        const secs = parts.length===2 ? parts[0]*60+parts[1] : parts[0]*3600+parts[1]*60+parts[2];
        window.open(`https://www.youtube.com/watch?v=${vId}&t=${secs}s`, '_blank');
    }
});
</script>
</body>
</html>"""
    # Вставляем предзагруженные данные
    final_html = html_template.replace('{preloaded_json}', preloaded_json).replace('{len(sorted_streams)}', str(len(sorted_streams)))

    # Минификация
    final_html = textwrap.dedent(final_html)
    final_html = "\n".join(line.rstrip() for line in final_html.splitlines() if line.strip())
    final_html = re.sub(r'>\s+<', '><', final_html)
    final_html = re.sub(r'\n+', '\n', final_html)

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(final_html)
    print(f"[HTML обновлён] {os.path.abspath(OUTPUT_HTML)}")


def run_parser():
    db_data = load_database()
    is_first_run = len(db_data) == 0
    flat_opts = {
        'playlistend': None if is_first_run else NEW_STREAMS_TO_CHECK,
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
    }
    try:
        with YoutubeDL(flat_opts) as ydl:
            print("1. Получение списка трансляций через yt-dlp...")
            channel_data = ydl.extract_info(CHANNEL_STREAMS_URL, download=False)
            if not channel_data or 'entries' not in channel_data:
                print("Не удалось получить список стримов.")
                return

            videos_to_parse = []
            for entry in channel_data['entries']:
                if not entry:
                    continue
                video_id = entry.get('id')
                if not video_id:
                    continue
                if video_id in db_data and db_data[video_id].get('list_type') == 'tracklist':
                    continue
                videos_to_parse.append({
                    'id': video_id,
                    'title': entry.get('title', 'Без названия')
                })

            if not videos_to_parse:
                print("Новых трансляций нет. База актуальна.")
                generate_html_report()
                all_streams = sorted(db_data.values(), key=lambda x: x.get("raw_date", "00000000"), reverse=True)
                generate_sitemap(all_streams)
                generate_robots_txt()
                return

            print(f"2. Парсинг {len(videos_to_parse)} видео (комментарии через API)...")
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                executor.map(parse_single_video, videos_to_parse)

            print("\n3. Готово.")
            generate_html_report()
            db_updated = load_database()
            all_streams = sorted(db_updated.values(), key=lambda x: x.get("raw_date", "00000000"), reverse=True)
            generate_sitemap(all_streams)
            generate_robots_txt()
    except Exception as e:
        print(f"\nКРИТИЧЕСКАЯ ОШИБКА: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run_parser()
