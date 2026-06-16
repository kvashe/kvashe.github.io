import os
import re
import json
import threading
import textwrap
import requests
import sys
from yt_dlp import YoutubeDL
from concurrent.futures import ThreadPoolExecutor

# ==================== НАСТРОЙКИ ====================
API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
if not API_KEY:
    raise ValueError("Переменная окружения YOUTUBE_API_KEY не установлена")

SITE_URL = "https://kvashe.github.io"

CHANNEL_STREAMS_URL = 'https://www.youtube.com/@kvashenaya/streams'
DB_FILE = "parsed_streams_db.json"
OUTPUT_HTML = "index.html"
TRACKLISTS_HTML = "tracklists.html"

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


def generate_sitemap():
    """Генерация sitemap.xml с основными страницами сайта."""
    pages = [
        {"loc": f"{SITE_URL}/index.html", "priority": "1.0"},
        {"loc": f"{SITE_URL}/tracklists.html", "priority": "0.8"},
    ]
    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for p in pages:
        xml_lines.append("  <url>")
        xml_lines.append(f"    <loc>{p['loc']}</loc>")
        xml_lines.append(f"    <priority>{p['priority']}</priority>")
        xml_lines.append("  </url>")
    xml_lines.append("</urlset>")

    with open("sitemap.xml", "w", encoding="utf-8") as f:
        f.write("\n".join(xml_lines))
    print(f"[Sitemap создан] {os.path.abspath('sitemap.xml')}")


def generate_html_report():
    print("Генерация статического index.html, SEO-страницы tracklists.html и sitemap.xml...")

    # Загружаем базу для создания страницы треклистов
    db_data = load_database()
    streams_for_seo = [
        item for item in db_data.values()
        if item.get('list_type') in ('tracklist', 'mixed') and item.get('timecodes')
    ]
    streams_for_seo.sort(key=lambda x: x.get("raw_date", "00000000"), reverse=True)

    # Генерация tracklists.html (простой список для индексации)
    seo_lines = [
        '<!DOCTYPE html>',
        '<html lang="ru">',
        '<head>',
        '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
        '<title>Архив треклистов Квашеной – все треклисты</title>',
        '<meta name="description" content="Полный список всех треков с трансляций Квашеной. Таймкоды и названия песен.">',
        '<meta name="robots" content="index, follow">',
        '</head>',
        '<body>',
        '<h1>Все треклисты трансляций Квашеной</h1>'
    ]

    for stream in streams_for_seo:
        title = stream.get('title', 'Без названия')
        date = stream.get('date', '')
        seo_lines.append(f'<h2>{title} ({date})</h2>')
        seo_lines.append('<ul>')
        for line in stream.get('timecodes', []):
            safe_line = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            seo_lines.append(f'<li>{safe_line}</li>')
        seo_lines.append('</ul>')
    seo_lines.append('</body></html>')

    with open(TRACKLISTS_HTML, 'w', encoding='utf-8') as f:
        f.write('\n'.join(seo_lines))
    print(f"[SEO-страница создана] {os.path.abspath(TRACKLISTS_HTML)}")

    # Шаблон index.html с экранированными процентами
    html_template = r"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Архив трансляций Квашеной – треклисты песен</title>
    <meta name="description" content="Полный архив музыкальных треклистов с трансляций Квашеной. Удобный поиск песен по таймкодам.">
    <meta name="keywords" content="Квашеная, треклист, трансляции, музыка, песни, архив, таймкоды">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="%s/index.html">
    <meta property="og:title" content="Архив треклистов Квашеной">
    <meta property="og:description" content="Все песни с трансляций – поиск по трекам и таймкодам.">
    <meta property="og:type" content="website">
    <meta property="og:url" content="%s/index.html">
    <link rel="icon" href="favicon.ico" type="image/x-icon">
    <style>
        :root {
            --primary: #6366f1;
            --primary-hover: #4f46e5;
            --bg: #0b0f19;
            --card-bg: rgba(22, 28, 45, 0.6);
            --text-main: #f1f5f9;
            --text-muted: #94a3b8;
            --border: rgba(255, 255, 255, 0.08);
        }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 0 0 60px 0; color: var(--text-main); background: #0b1020; min-height: 100vh; overflow-x: hidden; -webkit-font-smoothing: antialiased; }
        .scroll-top { position: fixed; bottom: 30px; right: 30px; width: 48px; height: 48px; background: rgb(23 29 61 / 28%%); backdrop-filter: blur(8px); border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 50%%; color: white; font-size: 24px; cursor: pointer; display: flex; align-items: center; justify-content: center; opacity: 0; visibility: hidden; transition: all 0.3s ease; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3); z-index: 1000; }
        .scroll-top:hover { background: rgba(99, 102, 241, 0.9); border-color: rgba(255, 255, 255, 0.5); transform: translateY(-3px); box-shadow: 0 0 12px rgba(99, 102, 241, 0.6); }
        .scroll-top.show { opacity: 1; visibility: visible; }
        @media (max-width: 768px) { .scroll-top { bottom: 20px; right: 20px; width: 44px; height: 44px; font-size: 20px; } }
        .skeleton-row { background: linear-gradient(180deg, rgba(30, 38, 58, 0.8), rgba(22, 28, 45, 0.9)); border: 1px solid rgba(255,255,255,0.06); border-radius: 20px; padding: 24px; display: flex; flex-direction: row; gap: 24px; margin-bottom: 24px; position: relative; overflow: hidden; }
        .skeleton-img { width: 160px; height: 90px; background: #1e293b; border-radius: 12px; }
        .skeleton-content { flex: 1; display: flex; flex-direction: column; gap: 16px; }
        .skeleton-title { width: 70%%; height: 24px; background: #1e293b; border-radius: 8px; }
        .skeleton-details { width: 40%%; height: 20px; background: #1e293b; border-radius: 8px; }
        .shimmer { position: relative; overflow: hidden; }
        .shimmer::after { content: ''; position: absolute; top: 0; left: 0; width: 100%%; height: 100%%; background: linear-gradient(110deg, rgba(255,255,255,0) 0%%, rgba(255,255,255,0.06) 40%%, rgba(255,255,255,0) 60%%); animation: shimmerMove 1.2s infinite linear; pointer-events: none; }
        @keyframes shimmerMove { 0%% { transform: translateX(-100%%); } 100%% { transform: translateX(100%%); } }
        @media (max-width: 768px) { .skeleton-row { flex-direction: column; gap: 16px; } .skeleton-img { width: 100%%; aspect-ratio: 16/9; height: auto; } .skeleton-title { width: 85%%; } }
        .parallax-notes { position: fixed; top: 0; left: 0; width: 100%%; height: 100%%; overflow: hidden; pointer-events: none; z-index: 0; }
        .note { position: absolute; user-select: none; pointer-events: none; will-change: top; font-family: "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", sans-serif; text-shadow: 0 0 12px rgba(0,0,0,0.4); transition: top 0.1s linear; }
        .note-content { display: inline-block; animation: gentleFloat 6s infinite ease-in-out; will-change: transform; }
        @keyframes gentleFloat { 0%% { transform: translateY(0px); } 50%% { transform: translateY(-12px); } 100%% { transform: translateY(0px); } }
        body::before { content: ""; position: fixed; inset: 0; z-index: -10; pointer-events: none; background-image: radial-gradient(at 80%% 20%%, rgba(99, 102, 241, 0.15) 0px, transparent 50%%), radial-gradient(at 20%% 80%%, rgba(244, 63, 94, 0.1) 0px, transparent 50%%); background-repeat: no-repeat; }
        .container { max-width: 1000px; margin: 0 auto; padding: 0 24px; }
        .header-panel { position: sticky; top: 0; background: rgba(11, 15, 25, 0.75); border-bottom: 1px solid var(--border); z-index: 100; padding: 20px 0 12px 0; margin-bottom: 40px; box-shadow: 0 10px 30px -10px rgba(0,0,0,0.5); }
        .header-flex { display: flex; align-items: center; justify-content: space-between; gap: 20px; flex-wrap: wrap; }
        .header-flex h2 { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.25rem; white-space: nowrap; }
        .header-flex h2 span { white-space: nowrap; background: linear-gradient(135deg, #a5b4fc 0%%, #6366f1 100%%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        h2 { color: #fff; font-size: 24px; font-weight: 800; margin: 0; display: flex; align-items: center; gap: 12px; letter-spacing: -0.5px; }
        .search-box { flex-grow: 1; max-width: 400px; display: flex; flex-direction: column; align-items: flex-start; }
        .input-wrapper { position: relative; width: 100%%; display: flex; align-items: center; }
        .s-input { width: 100%%; padding: 12px 44px 12px 18px; font-size: 15px; border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; box-sizing: border-box; outline: none; transition: all 0.3s; background: rgba(15, 23, 42, 0.6); color: #fff; box-shadow: inset 0 2px 4px rgba(0,0,0,0.2); }
        .s-input:focus { border-color: var(--primary); box-shadow: 0 0 0 4px rgba(99, 102, 241, 0.25), inset 0 2px 4px rgba(0,0,0,0.2); background: rgba(15, 23, 42, 0.8); }
        #searchStats { color: var(--text-muted); font-size: 13px; font-weight: 600; letter-spacing: 0.3px; margin: 10px 0 4px 4px; pointer-events: none; }
        .s-clear-btn { position: absolute; right: 14px; background: rgba(255, 255, 255, 0.1); border: none; width: 22px; height: 22px; border-radius: 50%%; color: #94a3b8; font-size: 11px; font-weight: bold; cursor: pointer; display: none; align-items: center; justify-content: center; padding: 0; transition: all 0.2s; }
        .s-clear-btn:hover { background: rgba(255, 255, 255, 0.2); color: #fff; }
        .year-filters { display: flex; gap: 10px; margin-bottom: 30px; overflow-x: auto; white-space: nowrap; -webkit-overflow-scrolling: touch; padding-bottom: 8px; }
        .year-btn { background: rgba(30, 41, 59, 0.5); border: 1px solid rgba(255,255,255,0.06); padding: 10px 20px; border-radius: 10px; font-weight: 600; color: var(--text-muted); cursor: pointer; transition: all 0.2s; font-size: 14px; flex-shrink: 0; }
        .year-btn:hover { border-color: rgba(255,255,255,0.2); color: #fff; }
        .year-btn.active { background: var(--primary); border-color: var(--primary); color: #fff; box-shadow: 0 4px 12px rgba(99, 102, 241, 0.3); }
        .grid { display: flex; flex-direction: column; gap: 24px; }
        .grid:empty { min-height: 60vh; }
        .row { position: relative; display: flex; flex-direction: row; gap: 24px; padding: 24px; align-items: flex-start; background: linear-gradient(180deg, rgba(20,28,48,0.88), rgba(15,22,40,0.94)); border: 1px solid rgba(255,255,255,0.06); border-radius: 20px; box-shadow: 0 4px 20px rgba(0,0,0,0.15); transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1), border-color 0.3s, box-shadow 0.3s; overflow: hidden; z-index: 1; }
        .row::before { content: ""; position: absolute; top: 0; left: 0; right: 0; bottom: 0; background-image: var(--bg-thumb); background-size: 120%%; background-position: center; filter: blur(40px) brightness(0.25) saturate(1.4); opacity: 0.55; transition: opacity 0.3s; z-index: -1; pointer-events: none; }
        .row * { position: relative; z-index: 2; }
        .row:hover { transform: translateY(-2px); border-color: rgba(255, 255, 255, 0.15); box-shadow: 0 12px 30px rgba(0,0,0,0.3); }
        .row:hover::before { opacity: 0.6; }
        .v-date { position: absolute; top: 24px; right: 24px; color: var(--text-muted); font-size: 13px; font-weight: 700; background: rgba(15, 23, 42, 0.6); padding: 6px 12px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.06); letter-spacing: 0.5px; z-index: 3; }
        .v-content-block { display: flex; flex-direction: column; gap: 16px; flex-grow: 1; padding-right: 110px; }
        .v-link { text-decoration: none; flex-shrink: 0; }
        .v-title-link { text-decoration: none; align-self: flex-start; }
        .v-title-link:hover .v-title { color: #fff; text-shadow: 0 0 10px rgba(255,255,255,0.1); }
        .img-container { position: relative; width: 160px; height: 90px; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.3); background: #151c2d; border: 1px solid rgba(255,255,255,0.05); transform: translateZ(0); }
        .img-container::before { content: ""; position: absolute; top: 0; left: 0; width: 100%%; height: 100%%; background: #1e293b; z-index: 0; }
        .img-container img { width: 101%%; height: 100%%; object-fit: cover; display: block; transition: transform 0.3s, opacity 0.2s; position: relative; z-index: 1; }
        .img-container:hover img { transform: scale(1.05); }
        .play-overlay { position: absolute; top: 0; left: 0; width: 100%%; height: 100%%; background: rgba(15, 23, 42, 0.75); color: #fff; font-size: 12px; display: flex; align-items: center; justify-content: center; opacity: 0; transition: opacity 0.2s; font-weight: bold; z-index: 2; }
        .img-container:hover .play-overlay { opacity: 1; }
        .v-title { font-size: 18px; color: #f8fafc; font-weight: 700; line-height: 1.4; transition: color 0.2s; overflow-wrap: break-word; }
        .v-tcs { width: 100%%; max-width: 600px; }
        details { background: rgba(15, 23, 42, 0.4); padding: 12px 18px; border-radius: 12px; border: 1px solid rgba(99, 102, 241, 0.4); transition: all 0.2s; }
        details[open] { background: rgba(15, 23, 42, 0.7); border-color: rgba(99, 102, 241, 0.4); }
        summary { font-weight: 600; cursor: pointer; color: #cbd5e1; outline: none; user-select: none; font-size: 14px; list-style: none; }
        summary::-webkit-details-marker { display: none; }
        .summary-flex { display: flex; align-items: center; justify-content: space-between; gap: 15px; }
        .summary-flex span:first-child::before { content: "▼ "; font-size: 9px; color: var(--text-muted); display: inline-block; transition: transform 0.2s; margin-right: 8px; transform: rotate(-90deg); }
        details[open] summary .summary-flex span:first-child::before { transform: rotate(0deg); }
        .tc-list { margin-top: 16px; line-height: 1.7; max-height: 280px; overflow-y: auto; padding-right: 8px; position: relative; transition: opacity 0.3s ease, transform 0.3s ease; }
        .tc-list::-webkit-scrollbar { width: 4px; }
        .tc-list::-webkit-scrollbar-track { background: transparent; }
        .tc-list::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 10px; }
        .tc-list::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.3); }
        .no-tc-block { background: rgba(15, 23, 42, 0.4); padding: 12px 18px; border-radius: 12px; border: 1px solid rgba(99, 102, 241, 0.4); cursor: default; display: none; }
        .no-tc-block span { font-weight: 600; color: #cbd5e1; font-size: 14px; font-style: normal; }
        .t-click { background: rgba(99, 102, 241, 0.15); color: #a5b4fc; padding: 3px 10px; border-radius: 6px; font-weight: 700; cursor: pointer; margin-right: 12px; display: inline-block; font-size: 13px; transition: all 0.2s; font-variant-numeric: tabular-nums; border: 1px solid rgba(99, 102, 241, 0.2); }
        .t-click:hover { background: var(--primary); color: #fff; border-color: var(--primary); box-shadow: 0 0 10px rgba(99,102,241,0.4); }
        .tc-item { margin-bottom: 10px; border-bottom: 1px dashed rgba(255, 255, 255, 0.15); padding-bottom: 8px; font-size: 14px; display: flex; align-items: center; }
        .tc-item:last-of-type { border-bottom: none; margin-bottom: 0; padding-bottom: 0; }
        .s-title { color: #e2e8f0; }
        .badge { display: inline-block; padding: 5px 12px; font-size: 12px; font-weight: 600; border-radius: 8px; }
        .badge-tracklist { background: rgba(16, 185, 129, 0.1); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.2); }
        .badge-mixed { background: rgba(245, 158, 11, 0.1); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.2); }
        mark { background: rgba(139, 92, 246, 0.35); color: #ffffff; padding: 1px 1px; border-radius: 6px; border: 1px solid rgba(196, 181, 253, 0.6); text-shadow: 0 0 6px rgba(139, 92, 246, 0.8); }
        .hide { display: none; }
        .gray { color: var(--text-muted); font-size: 14px; font-style: italic; }
        .no-tcs-box { padding: 4px 0; }
        .tc-author { font-size: 11px; color: var(--text-muted); margin-top: 10px; text-align: right; font-style: italic; opacity: 0.65; letter-spacing: 0.3px; }
        @media (max-width: 768px) { .header-flex { flex-direction: column; align-items: flex-start; gap: 14px; } .search-box { width: 100%%; max-width: 100%%; } .row { flex-direction: column; gap: 16px; padding: 20px; } .row::before { z-index: -1 !important; filter: blur(45px) saturate(0.9) !important; opacity: 0.5 !important; } .v-date { position: static; margin-bottom: 0; font-size: 12px; align-self: flex-start; padding: 4px 8px; z-index: 2; } .v-content-block { padding-right: 0; margin-top: 0; gap: 12px; z-index: 2; width: 100%%; } .v-link { display: block; width: 100%%; z-index: 2; } .img-container { width: 100%%; height: auto; aspect-ratio: 16/9; border-radius: 14px; overflow: hidden; } .img-container img { z-index: 1 !important; } .play-overlay { display: none !important; } .v-title { font-size: 16px; } .v-tcs { max-width: 100%%; width: 100%%; } details { margin: 0 -20px; border-left: none; border-right: none; border-radius: 0; padding: 12px 20px; transition: none !important; } details[open] { border-bottom: none; background: rgba(15,23,42,0.8); } .no-tc-block { margin: 0 -20px; border-left: none; border-right: none; border-radius: 0; padding: 12px 20px; display: none; } .no-tc-block span { font-weight: 600; color: #cbd5e1; font-size: 14px; font-style: normal; } .tc-list { padding-left: 8px; padding-right: 8px; max-height: 220px; overflow-y: auto; scroll-behavior: auto; -webkit-overflow-scrolling: touch; } .t-click { margin-right: 6px; } .header-panel { position: relative; } summary { -webkit-tap-highlight-color: transparent; } }
    </style>
</head>
<body>
<!-- Скрытая ссылка для поисковиков на полный список треклистов -->
<a href="tracklists.html" style="display:none;" aria-hidden="true">Полный список треков (SEO)</a>

<div class="parallax-notes" id="parallaxNotes"></div>
<div class="header-panel">
    <div class="container header-flex">
        <h2>Архив трансляций <span>Квашеной</span></h2>
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
(function() {
    const notesContainer = document.getElementById('parallaxNotes');
    if (!notesContainer) return;
    const noteSymbols = ['♪', '♫', '♩', '🎵', '🎶', '𝄞', '♬', '🎙️', '🎸', '🎹'];
    const notesCount = 10;
    const notes = [];
    for (let i = 0; i < notesCount; i++) {
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
        contentSpan.style.color = `rgba(255, 255, 255, ${opacity * 0.9})`;
        const animDuration = 4 + Math.random() * 6;
        contentSpan.style.animation = `gentleFloat ${animDuration}s infinite ease-in-out`;
        contentSpan.style.animationDelay = `${Math.random() * 3}s`;
        noteDiv.appendChild(contentSpan);
        noteDiv.style.left = left + '%%';
        noteDiv.style.top = top + '%%';
        noteDiv.style.transform = `rotate(${rotation}deg)`;
        notesContainer.appendChild(noteDiv);
        notes.push({ element: noteDiv, baseTop: parseFloat(top), parallaxFactor });
    }
    let ticking = false;
    function updateNotesPosition() {
        const scrollY = window.scrollY;
        const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
        const scrollProgress = maxScroll > 0 ? scrollY / maxScroll : 0;
        for (let n of notes) {
            const shiftPercent = (scrollProgress - 0.5) * n.parallaxFactor * 16;
            let newTop = n.baseTop + shiftPercent;
            newTop = Math.min(Math.max(newTop, -5), 105);
            n.element.style.top = newTop + '%%';
        }
        ticking = false;
    }
    window.addEventListener('scroll', () => { if (!ticking) { requestAnimationFrame(updateNotesPosition); ticking = true; } });
    window.addEventListener('resize', () => updateNotesPosition());
    updateNotesPosition();
})();

// ========== БЕЗОПАСНОЕ УДАЛЕНИЕ ТОЛЬКО ЭМОДЗИ ==========
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

// ========== НОРМАЛИЗАЦИЯ И СИНОНИМЫ ==========
function normalize(str) {
    return str.toLowerCase()
              .replace(/ë/g, 'e')
              .replace(/[^a-zа-яё0-9]/g, '');
}

const SYNONYMS = {
    "noizemc": ["noizemc", "noizemc", "нойзмс", "нойзмс", "noize", "нойз"],
    "rammstein": ["rammstein", "раммштайн"],
    "корольишут": ["корольишут", "киш"],
    "витас": ["витас", "vitas"],
    "ladygaga": ["ladygaga", "ледигага"],
    "максим": ["максим", "макsим"],
    "fleur": ["fleur", "flëur"],
    "nautiluspompilius": ["nautiluspompilius", "наутилуспомпилиус", "pompilius", "nautilus", "наутилус", "помпилиус"],
    "океанэлзи": ["океанэлзи", "элзи", "океанэльзы", "эльзы", "океанельзи", "ельзи"]
};

const variantToCanon = new Map();
for (const [canon, variants] of Object.entries(SYNONYMS)) {
    for (const v of variants) {
        variantToCanon.set(v, canon);
    }
}

function getSearchVariants(query) {
    const normQuery = normalize(query);
    if (variantToCanon.has(normQuery)) {
        const canon = variantToCanon.get(normQuery);
        return SYNONYMS[canon];
    }
    return [normQuery];
}

function matchesWithVariants(textNorm, variants) {
    for (let v of variants) {
        const normVariant = normalize(v);
        if (textNorm.includes(normVariant)) return true;
    }
    return false;
}

function highlightFirstMatch(original, variants) {
    if (!variants || variants.length === 0) return original;
    const normOriginal = normalize(original);
    let bestMatch = null;
    let bestIndex = Infinity;
    for (let v of variants) {
        const idx = normOriginal.indexOf(v);
        if (idx !== -1 && idx < bestIndex) {
            bestIndex = idx;
            bestMatch = v;
        }
    }
    if (bestMatch === null) return original;
    let origIdx = 0, normIdx = 0;
    while (normIdx < bestIndex && origIdx < original.length) {
        const ch = original[origIdx];
        const nch = normalize(ch);
        if (nch.length > 0) normIdx++;
        origIdx++;
    }
    const startOrig = origIdx;
    while (normIdx < bestIndex + bestMatch.length && origIdx < original.length) {
        const ch = original[origIdx];
        const nch = normalize(ch);
        if (nch.length > 0) normIdx++;
        origIdx++;
    }
    const endOrig = origIdx;
    return original.substring(0, startOrig) + '<mark>' + original.substring(startOrig, endOrig) + '</mark>' + original.substring(endOrig);
}

let streamsData = [];
let songsDB = {};
let searchIndex = [];
let activeYear = 'all';
let allRows = [];

function getTimecodeSeconds(line) {
    const match = line.match(/(\d{1,2}:\d{2}(?::\d{2})?)/);
    if (!match) return 99999999;
    const parts = match[1].split(':').map(Number);
    if (parts.length === 2) return parts[0]*60 + parts[1];
    if (parts.length === 3) return parts[0]*3600 + parts[1]*60 + parts[2];
    return 99999999;
}

function normalizeTimecode(tc) {
    const parts = tc.split(':').map(Number);
    if (parts.length === 2) return `00:${parts[0].toString().padStart(2,'0')}:${parts[1].toString().padStart(2,'0')}`;
    if (parts.length === 3) return parts.map(p => p.toString().padStart(2,'0')).join(':');
    return tc;
}

// ========== ЗАГРУЗКА ДАННЫХ (показываем tracklist и mixed) ==========
async function loadDatabase() {
    if (streamsData.length > 0) return;
    try {
        const response = await fetch('parsed_streams_db.json');
        if (!response.ok) throw new Error('Файл базы не найден');
        let rawData = await response.json();
        rawData.sort((a,b) => (b.raw_date || '00000000').localeCompare(a.raw_date || '00000000'));
        streamsData = rawData.filter(entry => entry.list_type === 'tracklist' || entry.list_type === 'mixed');
        songsDB = {};
        searchIndex = [];
        streamsData.forEach(entry => {
            const vId = entry.id;
            const timecodes = entry.timecodes || [];
            const sorted = timecodes.slice().sort((a,b) => getTimecodeSeconds(a) - getTimecodeSeconds(b));
            const tracks = sorted.map(line => {
                const match = line.match(/(\d{1,2}:\d{2}(?::\d{2})?)/);
                if (match) {
                    let s = line.replace(match[1], '').trim();
                    s = s.replace(/^[-–—]\s*/, '');
                    s = removeSpecificEmojis(s);
                    return { t: match[1], s: s };
                } else {
                    let s = line;
                    s = removeSpecificEmojis(s);
                    return { s: s };
                }
            });
            songsDB[vId] = { tracks, author: entry.author || '' };
            tracks.forEach(tr => {
                const norm = normalize(tr.s || '');
                searchIndex.push({ id: vId, text: tr.s, norm: norm });
            });
        });
    } catch(e) { console.error('Ошибка загрузки базы:', e); streamsData = []; }
}

// ========== РЕНДЕР ОДНОГО СТРИМА (с разными бейджами) ==========
function renderStreamHTML(stream) {
    const vId = stream.id;
    const title = stream.title || 'Без названия';
    const date = stream.date || 'Неизвестно';
    const rawYear = (stream.raw_date || '0000').substring(0,4);
    const timecodes = stream.timecodes || [];
    const listType = stream.list_type || 'none';
    const hasTracks = timecodes.length > 0;
    const badgeClass = `badge-${listType}`;
    const badgeText = listType === 'tracklist' ? 'Готовый трек-лист' : (listType === 'mixed' ? 'Сборный список' : '');
    const tcsHTML = hasTracks ? `<details><summary><div class="summary-flex"><span>Треклист ${timecodes.length}</span><span class="badge ${badgeClass}">${badgeText}</span></div></summary><div class="tc-list"></div></details>` : '<div class="no-tc-block"><span>Треклист не найден</span></div>';
    return `<div class="row" data-id="${vId}" data-year="${rawYear}" style="--bg-thumb: url('https://img.youtube.com/vi/${vId}/hqdefault.jpg');"><span class="v-date">${date}</span><a class="v-link" href="https://www.youtube.com/watch?v=${vId}" target="_blank"><div class="img-container"><img loading="lazy" decoding="async" src="https://img.youtube.com/vi/${vId}/hqdefault.jpg" alt="" onerror="this.style.opacity='0';"><span class="play-overlay">▶ Смотреть</span></div></a><div class="v-content-block"><a class="v-title-link" href="https://www.youtube.com/watch?v=${vId}" target="_blank"><span class="v-title">${title}</span></a><div class="v-tcs">${tcsHTML}</div></div></div>`;
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
    allRows = [...grid.querySelectorAll('.row')];
    document.querySelectorAll('details').forEach(details => {
        details.addEventListener('toggle', function() {
            const container = this.querySelector('.tc-list');
            if (!container) return;
            if (this.open) {
                const row = this.closest('.row');
                const vId = row.getAttribute('data-id');
                const filter = searchInput.value.toLowerCase().trim();
                if (container.children.length === 0) renderTracklist(vId, container, filter);
                animateContainer(container);
                if (filter) {
                    setTimeout(() => {
                        const mark = container.querySelector('mark');
                        if (mark) {
                            const item = mark.closest('.tc-item');
                            if (item) container.scrollTop = item.offsetTop - container.offsetTop - 10;
                        }
                    }, 50);
                }
            } else {
                container.style.transition = 'none';
                container.style.opacity = '0';
                container.style.transform = 'translateY(-10px)';
            }
        });
    });
}

function initYearFilters() {
    const yearsSet = new Set();
    allRows.forEach(row => { const y = row.getAttribute('data-year'); if(y && y !== '0000') yearsSet.add(y); });
    const sortedYears = Array.from(yearsSet).sort().reverse();
    const container = document.getElementById('yearFilters');
    let html = '<button class="year-btn active" data-year="all">Все годы</button>';
    sortedYears.forEach(y => html += `<button class="year-btn" data-year="${y}">${y} года</button>`);
    container.innerHTML = html;
    container.addEventListener('click', (e) => {
        if(e.target.classList.contains('year-btn')) {
            document.querySelectorAll('.year-btn').forEach(b => b.classList.remove('active'));
            e.target.classList.add('active');
            activeYear = e.target.getAttribute('data-year');
            executeSearch(searchInput.value.toLowerCase().trim());
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
            if (matchesWithVariants(normText, variants)) {
                sText = highlightFirstMatch(sText, variants);
            }
            const displayedTime = tr.t ? normalizeTimecode(tr.t) : '';
            html += tr.t ? `<div class="tc-item"><span class="t-click" data-time="${tr.t}">${displayedTime}</span><span class="s-title">${sText}</span></div>` : `<div class="tc-item"><span class="s-title">${sText}</span></div>`;
        });
    } else {
        tracks.forEach(tr => {
            const displayedTime = tr.t ? normalizeTimecode(tr.t) : '';
            html += tr.t ? `<div class="tc-item"><span class="t-click" data-time="${tr.t}">${displayedTime}</span><span class="s-title">${tr.s}</span></div>` : `<div class="tc-item"><span class="s-title">${tr.s}</span></div>`;
        });
    }
    const author = songsDB[vId]?.author;
    if (author && author.trim() !== '') html += `<div class="tc-author">Автор треклиста: ${author}</div>`;
    container.innerHTML = html;
}

const searchInput = document.getElementById('sInput');
const clearBtn = document.getElementById('sClear');
const statsEl = document.getElementById('searchStats');

async function executeSearch(filter) {
    if (allRows.length === 0) return;
    let visibleCount = 0;
    const variants = filter ? getSearchVariants(filter) : [];
    clearBtn.style.display = filter ? 'flex' : 'none';
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
    searchIndex.forEach(item => {
        if (matchesWithVariants(item.norm, variants)) matchedIds.add(item.id);
    });
    const isDesktop = window.innerWidth > 768;
    allRows.forEach(row => {
        const vId = row.getAttribute('data-id');
        const rowYear = row.getAttribute('data-year');
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
                setTimeout(() => {
                    const mark = tcList.querySelector('mark');
                    if (mark) {
                        const item = mark.closest('.tc-item');
                        if (item) tcList.scrollTop = item.offsetTop - tcList.offsetTop - 10;
                    }
                }, 50);
            }
        }
    });
    statsEl.textContent = 'Найдено трансляций: ' + visibleCount;
}

const scrollBtn = document.getElementById('scrollTopBtn');
window.addEventListener('scroll', () => { if (window.scrollY > 300) scrollBtn.classList.add('show'); else scrollBtn.classList.remove('show'); });
scrollBtn.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));

document.addEventListener('DOMContentLoaded', async () => {
    await loadDatabase();
    renderAllStreams();
    initYearFilters();
    executeSearch('');
});

document.getElementById('mainGrid').addEventListener('click', (e) => {
    if (e.target.classList.contains('t-click')) {
        e.preventDefault();
        const time = e.target.getAttribute('data-time');
        const vId = e.target.closest('.row').getAttribute('data-id');
        const parts = time.split(':').map(Number);
        const secs = parts.length === 2 ? parts[0]*60 + parts[1] : parts[0]*3600 + parts[1]*60 + parts[2];
        window.open(`https://www.youtube.com/watch?v=${vId}&t=${secs}s`, '_blank');
    }
});

let debounceTimer;
searchInput.addEventListener('input', () => { clearTimeout(debounceTimer); debounceTimer = setTimeout(() => executeSearch(searchInput.value.toLowerCase().trim()), 700); });
clearBtn.addEventListener('click', () => { searchInput.value = ''; clearBtn.style.display = 'none'; executeSearch(''); });
</script>
</body>
</html>""" % (SITE_URL, SITE_URL)

    # Минификация HTML и запись
    html_content = textwrap.dedent(html_template)
    html_content = "\n".join(line.rstrip() for line in html_content.splitlines() if line.strip())
    html_content = re.sub(r'>\s+<', '><', html_content)
    html_content = re.sub(r'\n+', '\n', html_content)

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"[HTML обновлён] {os.path.abspath(OUTPUT_HTML)}")

    # Генерация карты сайта
    generate_sitemap()


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
                return

            print(f"2. Парсинг {len(videos_to_parse)} видео (комментарии через API)...")
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                executor.map(parse_single_video, videos_to_parse)

            print("\n3. Готово.")
            generate_html_report()
    except Exception as e:
        print(f"\nКРИТИЧЕСКАЯ ОШИБКА: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run_parser()
