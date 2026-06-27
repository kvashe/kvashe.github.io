#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Парсер треклистов YouTube-трансляций
Оптимизированная версия с dataclass, единым API-клиентом, Retry-адаптером и вынесенными HTML-шаблонами
"""

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
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Set, Tuple, Any
from datetime import date
from yt_dlp import YoutubeDL
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==================== КОНСТАНТЫ ====================
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
LAST_SPONSOR_CHECK_FILE = "last_sponsor_check.txt"

# ===== Скомпилированные регулярки =====
TIMECODE_RE = re.compile(r'\b(?:\d{1,2}:)?\d{1,2}:\d{2}\b')
DASH_RE = re.compile(r'[-–—]')
DASH_PREFIX_RE = re.compile(r'^\s*[-–—]\s*')
NON_WORD_RE = re.compile(r'[^\w\sа-яА-ЯёЁA-Za-z]')
BAD_WORDS_RE = re.compile(r'\b(?:стрим|волос|сигна|говорит|чат|вопрос|talking|спросить|умница|красив)\b', re.IGNORECASE)
LETTERS_RE = re.compile(r'[A-Za-z]')
DURATION_RE = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?')

# ===== Оптимизированные списки для проверок =====
FORBIDDEN_PHRASES = (
    "хватит брать высокие ноты",
    "пой без кривляний",
    "но вот играть на ней ты явно неумеешь",
)
FORBIDDEN_PHRASES_LOWER = tuple(p.lower() for p in FORBIDDEN_PHRASES)

SPECIAL_ALLOWED = (
    "титры", "интро", "начало", "конец", "финал",
    "донаты", "розыгрыш", "чат", "сигн",
    "вступление", "intro", "outro", "припев", "куплет"
)

BANNED_WORDS = (
    "привет", "ага", "сегодня", "ладно", "понятно", "ок", "ну", "блин"
)

# ===== Сессия requests с Retry =====
SESSION = requests.Session()

# Настраиваем Retry стратегию
retry_strategy = Retry(
    total=5,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"]
)

adapter = HTTPAdapter(max_retries=retry_strategy)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)

# ==================== DATACLASS ====================
@dataclass(slots=True)
class VideoData:
    """Модель данных для видео с треклистом"""
    id: str
    title: str
    published_date: date
    timecodes: List[str]
    list_type: str
    author: str
    duration: int
    is_sponsor: bool = False

    def to_dict(self) -> dict:
        """Преобразует объект в словарь для JSON сериализации"""
        data = asdict(self)
        data['published_date'] = self.published_date.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> 'VideoData':
        """Создает объект из словаря"""
        if 'published_date' in data:
            if isinstance(data['published_date'], str):
                data['published_date'] = datetime.datetime.fromisoformat(data['published_date']).date()
            elif isinstance(data['published_date'], date):
                pass
        return cls(**data)
    
    def get_formatted_date(self) -> str:
        """Возвращает дату в формате DD.MM.YYYY для отображения"""
        return self.published_date.strftime("%d.%m.%Y")

@dataclass(slots=True)
class ParsedTimecode:
    """Модель для парсинга одной строки таймкода"""
    start: int
    end: Optional[int]
    title: str
    raw: str

# ==================== YOUTUBE API КЛИЕНТ ====================
class YouTubeAPI:
    """Единый клиент для работы с YouTube API"""
    
    BASE_URL = "https://www.googleapis.com/youtube/v3/"
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = SESSION
    
    def _request(self, endpoint: str, params: dict, timeout: int = 10) -> dict:
        """Базовый метод для всех API-запросов"""
        params["key"] = self.api_key
        
        try:
            resp = self.session.get(
                self.BASE_URL + endpoint,
                params=params,
                timeout=timeout
            )
            
            if resp.status_code == 403:
                logging.warning("Quota exceeded или доступ запрещён для %s", endpoint)
                return {}
            
            resp.raise_for_status()
            return resp.json()
            
        except requests.exceptions.Timeout:
            logging.error("Таймаут запроса к %s", endpoint)
            return {}
        except requests.exceptions.RequestException as e:
            logging.error("Ошибка API при запросе к %s: %s", endpoint, e)
            return {}
    
    def get_videos_info(self, video_ids: List[str]) -> Dict[str, dict]:
        """Получает информацию о видео одним запросом"""
        if not video_ids:
            return {}
        
        all_info = {}
        total_batches = (len(video_ids) + 49) // 50
        
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i+50]
            batch_num = i // 50 + 1
            
            logging.debug("Получение информации для batch %d/%d (%d видео)...", batch_num, total_batches, len(batch))
            
            data = self._request(
                "videos",
                {
                    "part": "snippet,contentDetails,statistics",
                    "id": ",".join(batch)
                }
            )
            
            if not data:
                logging.warning("Не удалось получить информацию для batch %d", batch_num)
                continue
            
            for item in data.get("items", []):
                video_id = item["id"]
                
                published_at = item.get("snippet", {}).get("publishedAt", "")
                if published_at:
                    published_date = datetime.datetime.fromisoformat(published_at.replace('Z', '+00:00')).date()
                else:
                    published_date = date(1970, 1, 1)
                
                duration_iso = item.get("contentDetails", {}).get("duration", "")
                duration = 0
                if duration_iso:
                    match = DURATION_RE.match(duration_iso)
                    if match:
                        h = int(match.group(1) or 0)
                        m = int(match.group(2) or 0)
                        s = int(match.group(3) or 0)
                        duration = h*3600 + m*60 + s
                
                stats = item.get("statistics", {})
                is_sponsor = "viewCount" not in stats
                
                all_info[video_id] = {
                    "published_date": published_date,
                    "duration": duration,
                    "is_sponsor": is_sponsor,
                    "available": not is_sponsor
                }
                
                logging.debug("✅ %s: дата=%s, длит=%dс, спонсор=%s", 
                             video_id, published_date, duration, is_sponsor)
            
            returned_ids = {item["id"] for item in data.get("items", [])}
            missing = set(batch) - returned_ids
            if missing:
                logging.debug("❌ %d видео отсутствуют в ответе API (удалены или недоступны)", len(missing))
        
        return all_info
    
    def get_comments(self, video_id: str, max_comments: int = 3000) -> List[dict]:
        """Получает комментарии к видео"""
        comments = []
        page_token = None

        while len(comments) < max_comments:
            data = self._request(
                "commentThreads",
                {
                    "videoId": video_id,
                    "part": "snippet",
                    "maxResults": 100,
                    "textFormat": "plainText",
                    "pageToken": page_token
                }
            )
            
            if not data:
                logging.warning("Не удалось получить комментарии для %s", video_id)
                break

            if "error" in data:
                break

            for item in data.get("items", []):
                snippet = item["snippet"]["topLevelComment"]["snippet"]
                published_at = snippet.get("publishedAt", "")
                
                timestamp = 9999999999
                if published_at:
                    try:
                        timestamp = datetime.datetime.fromisoformat(
                            published_at.replace('Z', '+00:00')
                        ).timestamp()
                    except:
                        pass
                
                comments.append({
                    "text": snippet.get("textDisplay", ""),
                    "author": snippet.get("authorDisplayName", ""),
                    "is_pinned": False,
                    "published_at": published_at,
                    "timestamp": timestamp
                })
            
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        
        return comments
    
    def check_sponsors(self, video_ids: List[str]) -> Set[str]:
        """Проверяет пачку видео на спонсорство"""
        if not video_ids:
            return set()
        
        video_info = self.get_videos_info(video_ids)
        return {vid for vid, info in video_info.items() if info.get("available", False)}

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

def load_database(db_file) -> Dict[str, VideoData]:
    if not os.path.exists(db_file):
        return {}
    try:
        with open(db_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {item['id']: VideoData.from_dict(item) for item in data if 'id' in item}
        elif isinstance(data, dict):
            return {vid: VideoData.from_dict(item) for vid, item in data.items()}
        else:
            return {}
    except (json.JSONDecodeError, Exception) as e:
        logging.warning("Ошибка чтения базы (%s). Будет создана новая.", e)
        return {}

def save_database(db: Dict[str, VideoData], db_file):
    with db_lock:
        sorted_items = sorted(db.values(), key=lambda x: x.published_date, reverse=True)
        tmp_file = db_file + ".tmp"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump([item.to_dict() for item in sorted_items], f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, db_file)
        except Exception as e:
            logging.error("Ошибка сохранения базы: %s", e)
            if os.path.exists(tmp_file):
                os.unlink(tmp_file)

# ==================== ПРОВЕРКА СПОНСОРСКИХ ВИДЕО ====================
def should_run_sponsor_check():
    if not os.path.exists(LAST_SPONSOR_CHECK_FILE):
        return True

    try:
        with open(LAST_SPONSOR_CHECK_FILE, "r", encoding="utf-8") as f:
            last_check = f.read().strip()

        last_date = datetime.datetime.strptime(last_check, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        days_passed = (datetime.datetime.now(datetime.timezone.utc) - last_date).days

        return days_passed >= 30

    except Exception:
        return True

def update_sponsor_check_date():
    with open(LAST_SPONSOR_CHECK_FILE, "w", encoding="utf-8") as f:
        f.write(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"))

# ==================== ПАРСИНГ ТАЙМКОДОВ ====================
def timecode_to_seconds(time_str: str) -> int:
    parts = [int(p) for p in time_str.split(':')]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    elif len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 99999999

def parse_timecode(line: str) -> Optional[ParsedTimecode]:
    matches = list(TIMECODE_RE.finditer(line))
    if not matches:
        return None
    
    first = matches[0]
    start_sec = timecode_to_seconds(first.group(0))
    
    end_sec = None
    title = line[:first.start()] + line[first.end():]
    
    if len(matches) >= 2:
        second = matches[1]
        between = line[first.end():second.start()]
        if DASH_RE.search(between):
            end_sec = timecode_to_seconds(second.group(0))
            if end_sec > start_sec:
                title = line[:first.start()] + line[second.end():]
                title = DASH_PREFIX_RE.sub('', title).strip()
    
    title = title.strip()
    
    return ParsedTimecode(
        start=start_sec,
        end=end_sec,
        title=title,
        raw=line
    )

def is_transcript_like(parsed_list: List[ParsedTimecode], min_gap: int) -> bool:
    if len(parsed_list) < 5:
        return False
    
    times = [p.start for p in parsed_list]
    deltas = [times[i+1] - times[i] for i in range(len(times)-1) if times[i+1] > times[i]]
    
    if not deltas:
        return False
    
    return (sum(deltas) / len(deltas)) < min_gap

def is_good_timecode_line_parsed(parsed: ParsedTimecode, min_words: int, max_words: int) -> bool:
    after_time = parsed.title
    if not after_time:
        return False
    
    clean_after = NON_WORD_RE.sub(' ', after_time)
    words = clean_after.split()
    word_count = len(words)
    
    effective_max = 50 if ' - ' in after_time else max_words
    
    lower_after = after_time.lower()
    
    if word_count < min_words:
        if not any(word in lower_after for word in SPECIAL_ALLOWED):
            return False
    
    if word_count > effective_max:
        return False
    
    if lower_after in BANNED_WORDS:
        return False
    
    return True

def extract_smart_timecodes(comments, min_timecodes, min_words, max_words, min_gap, debug):
    candidates = []
    mixed_parsed = []
    mixed_authors = set()

    for comment in comments:
        text = comment.get('text', '')
        text_lower = text.lower()
        
        if any(p in text_lower for p in FORBIDDEN_PHRASES_LOWER):
            continue

        lines = [line.strip() for line in text.split('\n')]
        parsed_lines = []
        
        for line in lines:
            parsed = parse_timecode(line)
            if parsed:
                parsed_lines.append(parsed)
        
        if not parsed_lines:
            continue

        valid_parsed = [
            p for p in parsed_lines 
            if is_good_timecode_line_parsed(p, min_words, max_words)
        ]
        
        tc_count = len(valid_parsed)

        if tc_count >= min_timecodes:
            if is_transcript_like(valid_parsed, min_gap):
                continue

            music_score = 0
            for p in valid_parsed:
                if ' - ' in p.title:
                    music_score += 3
                if LETTERS_RE.search(p.title):
                    music_score += 1
                if '(' in p.title or ')' in p.title:
                    music_score += 1
                
                if BAD_WORDS_RE.search(p.title.lower()):
                    music_score -= 2

            author = comment.get('author', 'Неизвестно')
            if author in ("@ajoajo701", "@mirovoy100"):
                music_score = -1_000_000

            candidates.append({
                'text': text,
                'valid_parsed': valid_parsed,
                'tc_count': tc_count,
                'music_score': music_score,
                'author': author,
                'timestamp': comment.get('timestamp', 9999999999)
            })
        else:
            mixed_parsed.extend(valid_parsed)
            mixed_authors.add(comment.get('author', 'Неизвестно'))

    if candidates:
        best = max(candidates, key=lambda c: (c['music_score'], c['tc_count'], -c['timestamp']))
        
        dedup = {}
        for p in best['valid_parsed']:
            if p.start not in dedup:
                dedup[p.start] = p
        
        sorted_parsed = sorted(dedup.values(), key=lambda x: x.start)
        timecode_strings = [p.raw for p in sorted_parsed]
        
        return timecode_strings, "tracklist", best['author']

    if mixed_parsed:
        dedup_mixed = {}
        for p in mixed_parsed:
            if p.start not in dedup_mixed:
                dedup_mixed[p.start] = p
        
        sorted_parsed = sorted(dedup_mixed.values(), key=lambda x: x.start)
        timecode_strings = [p.raw for p in sorted_parsed]
        
        authors_str = ", ".join(list(mixed_authors)[:3])
        if len(mixed_authors) > 3:
            authors_str += " и др."
        
        return timecode_strings, "mixed", authors_str

    return [], "none", ""

# ==================== ПАРСИНГ ОДНОГО ВИДЕО ====================
def parse_single_video(video_entry, video_info: dict, db: Dict[str, VideoData], args, youtube: YouTubeAPI):
    """Парсит одно видео, используя уже полученную информацию о нем"""
    video_id = video_entry['id']
    title = video_entry.get('title', 'No Title')
    logging.info("Парсинг: %s...", title[:60])

    comments = youtube.get_comments(video_id)
    timecodes, list_type, author = extract_smart_timecodes(
        comments, args.min_timecodes, args.min_words, args.max_words, args.min_gap, args.debug
    )

    if args.force_author:
        author = args.force_author

    info = video_info.get(video_id, {})
    published_date = info.get("published_date", date(1970, 1, 1))
    duration = info.get("duration", 0)
    is_sponsor = info.get("is_sponsor", False)

    video_data = VideoData(
        id=video_id,
        title=title,
        published_date=published_date,
        timecodes=timecodes,
        list_type=list_type,
        author=author,
        duration=duration,
        is_sponsor=is_sponsor
    )
    
    with db_lock:
        db[video_id] = video_data
    
    logging.info("Сохранено: %s... (%s) автор=%s, треков=%d, длит=%s",
                 title[:50], published_date.strftime("%d.%m.%Y"), author, len(timecodes),
                 str(datetime.timedelta(seconds=duration)) if duration else "неизв")
    return True

# ==================== ГЕНЕРАЦИЯ HTML-ОТЧЕТОВ ====================
def generate_html_report(db: Dict[str, VideoData], site_url, output_html, tracklists_html, player_html_path):
    logging.info("Генерация HTML-отчётов...")

    streams_for_seo = [
        item for item in db.values()
        if item.list_type in ('tracklist', 'mixed') and item.timecodes
    ]
    streams_for_seo.sort(key=lambda x: x.published_date, reverse=True)
    
    seo_lines = [
        '<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">',
        '<title>Архив треклистов Квашеной – все треклисты</title>',
        '<meta name="robots" content="index, follow">',
        '</head><body>',
        '<h1>Все треклисты трансляций Квашеной</h1>'
    ]
    for stream in streams_for_seo:
        title = html.escape(stream.title)
        date = html.escape(stream.get_formatted_date())
        seo_lines.append(f'<h2>{title} ({date})</h2><ul>')
        for line in stream.timecodes:
            seo_lines.append(f'<li>{html.escape(line)}</li>')
        seo_lines.append('</ul>')
    seo_lines.append('</body></html>')
    
    with open(tracklists_html, 'w', encoding='utf-8') as f:
        f.write('\n'.join(seo_lines))
    logging.info("SEO-страница создана: %s", os.path.abspath(tracklists_html))

    with open(player_html_path, 'w', encoding='utf-8') as f:
        f.write(PLAYER_TEMPLATE)
    logging.info("player.html создан: %s", os.path.abspath(player_html_path))

    safe_site_url = html.escape(site_url)
    index_html = INDEX_TEMPLATE.replace('{site_url}', safe_site_url)
    index_html = minify_html(index_html)
    
    with open(output_html, 'w', encoding='utf-8') as f:
        f.write(index_html)
    logging.info("HTML обновлён: %s", os.path.abspath(output_html))

    with open("sitemap.xml", "w", encoding="utf-8") as f:
        f.write(SITEMAP_TEMPLATE.format(site_url=safe_site_url))
    logging.info("Sitemap создан: %s", os.path.abspath("sitemap.xml"))

def minify_html(html_content: str) -> str:
    html_content = re.sub(r'<!--.*?-->', '', html_content, flags=re.DOTALL)
    html_content = re.sub(r'>\s+<', '><', html_content)
    html_content = re.sub(r'\s{2,}', ' ', html_content)
    return html_content.strip()

# ==================== ГЛАВНАЯ ФУНКЦИЯ ====================
def run_parser():
    args = parse_args()
    debug_mode = args.debug if args.debug is not None else DEBUG
    setup_logging(debug_mode)

    api_key = args.api_key
    if not api_key:
        logging.error("API-ключ YouTube не указан. Задайте --api-key или переменную YOUTUBE_API_KEY")
        sys.exit(1)

    youtube = YouTubeAPI(api_key)
    
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
                if video_id in db:
                    if db[video_id].published_date == date(1970, 1, 1):
                        videos_to_fix_date.append(video_id)
                    continue
                videos_to_parse.append({
                    'id': video_id,
                    'title': entry.get('title', 'Без названия'),
                })

        if videos_to_fix_date:
            logging.info("Восстановление дат для %d существующих треклистов...", len(videos_to_fix_date))
            video_info = youtube.get_videos_info(videos_to_fix_date)
            
            for video_id in videos_to_fix_date:
                if video_id in video_info:
                    info = video_info[video_id]
                    published_date = info.get("published_date", date(1970, 1, 1))
                    if published_date != date(1970, 1, 1):
                        with db_lock:
                            if video_id in db:
                                db[video_id].published_date = published_date
                                logging.info("Дата обновлена для %s: %s", video_id, published_date.strftime("%d.%m.%Y"))
            save_database(db, args.db)

        if videos_to_parse:
            logging.info("Парсинг %d видео...", len(videos_to_parse))
            
            video_ids = [v["id"] for v in videos_to_parse]
            logging.info("Получение информации о %d видео одним запросом...", len(video_ids))
            all_video_info = youtube.get_videos_info(video_ids)

            def worker(video):
                parse_single_video(video, all_video_info, db, args, youtube)

            chunksize = max(1, min(10, len(videos_to_parse) // (args.max_workers * 2)))
            logging.info("Используем chunksize=%d для %d видео", chunksize, len(videos_to_parse))
            
            with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
                executor.map(worker, videos_to_parse, chunksize=chunksize)

            save_database(db, args.db)

        if should_run_sponsor_check():
            logging.info("Запуск ежемесячной проверки спонсорских видео")

            videos_to_check = [
                video_id for video_id, video_data in db.items()
                if video_data.list_type in ("tracklist", "mixed")
            ]
            
            logging.info("Найдено %d видео с треклистами в базе", len(videos_to_check))
            
            if videos_to_check:
                available_videos = youtube.check_sponsors(videos_to_check)
                
                logging.info("API вернул %d доступных видео из %d запрошенных",
                           len(available_videos), len(videos_to_check))
                
                sponsors_found = 0
                already_sponsored = 0
                still_available = 0
                
                for video_id in videos_to_check:
                    if video_id in available_videos:
                        if db[video_id].is_sponsor:
                            logging.info("Видео перестало быть спонсорским: %s", db[video_id].title)
                        db[video_id].is_sponsor = False
                        still_available += 1
                    else:
                        if not db[video_id].is_sponsor:
                            sponsors_found += 1
                            logging.info("Видео стало спонсорским: %s", db[video_id].title)
                        else:
                            already_sponsored += 1
                        db[video_id].is_sponsor = True
                
                update_sponsor_check_date()
                
                logging.info("Результаты проверки: доступно=%d, стало спонсорскими=%d, уже были спонсорскими=%d",
                           still_available, sponsors_found, already_sponsored)
            else:
                logging.info("Нет видео для проверки")
        else:
            logging.info("Ежемесячная проверка спонсорских видео не требуется (прошло меньше 30 дней)")

        save_database(db, args.db)

        logging.info("Генерация отчётов и плеера...")
        generate_html_report(db, args.site_url, args.output, args.tracklists, args.player)

    except Exception as e:
        logging.exception("Критическая ошибка")
        sys.exit(1)

# ==================== ШАБЛОНЫ HTML ====================
SITEMAP_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{site_url}/index.html</loc><priority>1.0</priority></url>
  <url><loc>{site_url}/tracklists.html</loc><priority>0.8</priority></url>
  <url><loc>{site_url}/player.html</loc><priority>0.7</priority></url>
</urlset>"""

PLAYER_TEMPLATE = r"""<!DOCTYPE html>
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
        <img class="artwork-image" id="artworkImage" src="" alt="Обложка видео" style="display:none;">
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
<div id="ytplayer" style="position:absolute;width:1px;height:1px;opacity:0.01;pointer-events:none;"></div>
<script src="https://www.youtube.com/iframe_api"></script>
<script>
// ===== ФУНКЦИЯ ФОРМАТИРОВАНИЯ ДАТЫ =====
function formatDateForDisplay(dateStr) {
    try {
        if (!dateStr) return '';
        // Если дата уже в формате DD.MM.YYYY, возвращаем как есть
        if (/^\d{2}\.\d{2}\.\d{4}$/.test(dateStr)) return dateStr;
        
        // Пробуем распарсить ISO формат (YYYY-MM-DD)
        var parts = dateStr.split('-');
        if (parts.length === 3) {
            var year = parts[0];
            var month = parts[1];
            var day = parts[2];
            // Проверяем, что это валидные числа
            if (!isNaN(year) && !isNaN(month) && !isNaN(day)) {
                return day + '.' + month + '.' + year;
            }
        }
        
        // Пробуем распарсить через Date
        var date = new Date(dateStr);
        if (!isNaN(date.getTime())) {
            var d = String(date.getDate()).padStart(2, '0');
            var m = String(date.getMonth() + 1).padStart(2, '0');
            var y = date.getFullYear();
            return d + '.' + m + '.' + y;
        }
        
        return dateStr;
    } catch(e) {
        return dateStr || '';
    }
}

// ===== ФУНКЦИИ ДЛЯ ОБЛОЖКИ =====
function onArtworkLoad() {
    var img = document.getElementById('artworkImage');
    var placeholder = document.getElementById('artworkPlaceholder');
    var container = document.getElementById('playerContainer');
    
    img.style.display = 'block';
    placeholder.style.display = 'none';
    
    if (img.src) {
        try {
            var color = extractColorFromImage(img);
            applyDynamicColor(color.r, color.g, color.b);
            container.style.setProperty('--bg-image', 'url(' + img.src + ')');
        } catch(e) {}
    }
}

function onArtworkError() {
    var img = document.getElementById('artworkImage');
    var placeholder = document.getElementById('artworkPlaceholder');
    img.style.display = 'none';
    placeholder.style.display = 'block';
}

// ===== ОСТАЛЬНОЙ JS КОД =====
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

// Добавляем обработчики событий для изображения
artworkImage.onload = onArtworkLoad;
artworkImage.onerror = onArtworkError;

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
                        streamDate: item.published_date || '',
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

window.onYouTubeIframeAPIReady = function() {
    try {
        player = new YT.Player('ytplayer', {
            height: '150',
            width: '200',
            playerVars: {
                autoplay: 1,
                playsinline: 1,
                mute: 0,
                rel: 0,
                modestbranding: 1,
                showinfo: 0,
                controls: 0,
                origin: window.location.origin
            },
            events: {
                onReady: onPlayerReady,
                onStateChange: onPlayerStateChange,
                onError: function(event) {
                    console.warn('YouTube player error:', event.data);
                    if (event.data === 100 || event.data === 101 || event.data === 150) {
                        setTimeout(function() {
                            if (currentTrack) {
                                player.loadVideoById({
                                    videoId: currentTrack.videoId,
                                    startSeconds: currentTrack.start
                                });
                            }
                        }, 2000);
                    }
                }
            }
        });
    } catch(e) {
        console.warn('YouTube iframe init error:', e);
    }
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
    var formattedDate = formatDateForDisplay(currentTrack.streamDate);
    setupMarquee(trackTitle, currentTrack.title);
    setupMarquee(streamInfo, currentTrack.streamTitle + ' \u00b7 ' + formattedDate);
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

var scrollBtn = document.getElementById('scrollTopBtn');
window.addEventListener('scroll', function() { 
    if (window.scrollY > 300) {
        if (scrollBtn) scrollBtn.classList.add('show');
    } else {
        if (scrollBtn) scrollBtn.classList.remove('show');
    }
}, { passive: true });

if (scrollBtn) {
    scrollBtn.addEventListener('click', function() {
        window.scrollTo({ top: 0, behavior: 'smooth' });
    });
}

if (typeof YT !== 'undefined' && YT.Player) window.onYouTubeIframeAPIReady();
window.addEventListener('load', init);
</script>
</body>
</html>"""

INDEX_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Архив трансляций Квашеной – треклисты песен</title>
    <meta name="description" content="Полный архив музыкальных треклистов с трансляций Квашеной. Удобный поиск песен по таймкодам.">
    <meta name="keywords" content="Квашеная, треклист, трансляции, музыка, песни, архив, таймкоды">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="{site_url}/index.html">
    <meta property="og:title" content="Архив треклистов Квашеной">
    <meta property="og:description" content="Все песни с трансляций – поиск по трекам и таймкодам.">
    <meta property="og:type" content="website">
    <meta property="og:url" content="{site_url}/index.html">
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
        .scroll-top { position: fixed; bottom: 30px; right: 30px; width: 48px; height: 48px; background: rgb(23 29 61 / 28%); backdrop-filter: blur(8px); border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 50%; color: white; font-size: 24px; cursor: pointer; display: flex; align-items: center; justify-content: center; opacity: 0; visibility: hidden; transition: all 0.3s ease; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3); z-index: 1000; }
        .scroll-top:hover { background: rgba(99, 102, 241, 0.9); border-color: rgba(255, 255, 255, 0.5); transform: translateY(-3px); box-shadow: 0 0 12px rgba(99, 102, 241, 0.6); }
        .scroll-top.show { opacity: 1; visibility: visible; }
        @media (max-width: 768px) { .scroll-top { bottom: 20px; right: 20px; width: 44px; height: 44px; font-size: 20px; } }
        .skeleton-row { background: linear-gradient(180deg, rgba(30, 38, 58, 0.8), rgba(22, 28, 45, 0.9)); border: 1px solid rgba(255,255,255,0.06); border-radius: 20px; padding: 24px; display: flex; flex-direction: row; gap: 24px; margin-bottom: 24px; position: relative; overflow: hidden; }
        .skeleton-img { width: 160px; height: 90px; background: #1e293b; border-radius: 12px; }
        .skeleton-content { flex: 1; display: flex; flex-direction: column; gap: 16px; }
        .skeleton-title { width: 70%; height: 24px; background: #1e293b; border-radius: 8px; }
        .skeleton-details { width: 40%; height: 20px; background: #1e293b; border-radius: 8px; }
        .shimmer { position: relative; overflow: hidden; }
        .shimmer::after { content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: linear-gradient(110deg, rgba(255,255,255,0) 0%, rgba(255,255,255,0.06) 40%, rgba(255,255,255,0) 60%); animation: shimmerMove 1.2s infinite linear; pointer-events: none; }
        @keyframes shimmerMove { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }
        @media (max-width: 768px) { .skeleton-row { flex-direction: column; gap: 16px; } .skeleton-img { width: 100%; aspect-ratio: 16/9; height: auto; } .skeleton-title { width: 85%; } }
        .parallax-notes { position: fixed; top: 0; left: 0; width: 100%; height: 100%; overflow: hidden; pointer-events: none; z-index: 0; }
        .note { position: absolute; user-select: none; pointer-events: none; will-change: top; font-family: "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", sans-serif; text-shadow: 0 0 12px rgba(0,0,0,0.4); transition: top 0.1s linear; }
        .note-content { display: inline-block; animation: gentleFloat 6s infinite ease-in-out; will-change: transform; }
        @keyframes gentleFloat { 0% { transform: translateY(0px); } 50% { transform: translateY(-12px); } 100% { transform: translateY(0px); } }
        body::before { content: ""; position: fixed; inset: 0; z-index: -10; pointer-events: none; background-image: radial-gradient(at 80% 20%, rgba(99, 102, 241, 0.15) 0px, transparent 50%), radial-gradient(at 20% 80%, rgba(244, 63, 94, 0.1) 0px, transparent 50%); background-repeat: no-repeat; }
        .container { max-width: 1000px; margin: 0 auto; padding: 0 24px; }
        .header-panel { position: sticky; top: 0; background: rgba(11, 15, 25, 0.75); border-bottom: 1px solid var(--border); z-index: 100; padding: 20px 0 12px 0; margin-bottom: 40px; box-shadow: 0 10px 30px -10px rgba(0,0,0,0.5); }
        .header-flex { display: flex; align-items: center; justify-content: space-between; gap: 20px; flex-wrap: wrap; }
        .header-flex h2 { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.25rem; white-space: nowrap; }
        .header-flex h2 span { white-space: nowrap; background: linear-gradient(135deg, #a5b4fc 0%, #6366f1 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        h2 { color: #fff; font-size: 24px; font-weight: 800; margin: 0; display: flex; align-items: center; gap: 12px; letter-spacing: -0.5px; }
        .search-box { flex-grow: 1; max-width: 400px; display: flex; flex-direction: column; align-items: flex-start; }
        .input-wrapper { position: relative; width: 100%; display: flex; align-items: center; }
        .s-input { width: 100%; padding: 12px 44px 12px 18px; font-size: 15px; border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; box-sizing: border-box; outline: none; transition: all 0.3s; background: rgba(15, 23, 42, 0.6); color: #fff; box-shadow: inset 0 2px 4px rgba(0,0,0,0.2); }
        .s-input:focus { border-color: var(--primary); box-shadow: 0 0 0 4px rgba(99, 102, 241, 0.25), inset 0 2px 4px rgba(0,0,0,0.2); background: rgba(15, 23, 42, 0.8); }
        #searchStats { color: var(--text-muted); font-size: 13px; font-weight: 600; letter-spacing: 0.3px; margin: 10px 0 4px 4px; pointer-events: none; }
        .s-clear-btn { position: absolute; right: 14px; background: rgba(255, 255, 255, 0.1); border: none; width: 22px; height: 22px; border-radius: 50%; color: #94a3b8; font-size: 11px; font-weight: bold; cursor: pointer; display: none; align-items: center; justify-content: center; padding: 0; transition: all 0.2s; }
        .s-clear-btn:hover { background: rgba(255, 255, 255, 0.2); color: #fff; }
        .sponsor-toggle {
            align-items: center;
            gap: 8px;
            margin-top: 8px;
            font-size: 13px;
            color: var(--text-muted);
            cursor: pointer;
            user-select: none;
        }
        .sponsor-toggle input[type="checkbox"] {
            display: none !important;
        }
        .sponsor-toggle .toggle-switch {
            width: 36px;
            height: 20px;
            background: rgba(255,255,255,0.1);
            border-radius: 10px;
            position: relative;
            transition: background 0.3s;
            flex-shrink: 0;
        }
        .sponsor-toggle .toggle-switch::after {
            content: '';
            position: absolute;
            top: 2px;
            left: 2px;
            width: 16px;
            height: 16px;
            background: #94a3b8;
            border-radius: 50%;
            transition: all 0.3s;
        }
        .sponsor-toggle input:checked + .toggle-switch {
            background: var(--primary);
        }
        .sponsor-toggle input:checked + .toggle-switch::after {
            left: 18px;
            background: white;
        }
        .row.sponsor {
            border-color: rgba(245, 158, 11, 0.3) !important;
            background: linear-gradient(180deg, rgba(30, 25, 15, 0.88), rgba(25, 20, 10, 0.94)) !important;
        }
        .row.sponsor::before {
            filter: blur(40px) brightness(0.15) saturate(0.5) sepia(0.5) !important;
            opacity: 0.4 !important;
        }
        .year-filters { display: flex; gap: 10px; margin-bottom: 30px; overflow-x: auto; white-space: nowrap; -webkit-overflow-scrolling: touch; padding-bottom: 8px; }
        .year-btn { background: rgba(30, 41, 59, 0.5); border: 1px solid rgba(255,255,255,0.06); padding: 10px 20px; border-radius: 10px; font-weight: 600; color: var(--text-muted); cursor: pointer; transition: all 0.2s; font-size: 14px; flex-shrink: 0; }
        .year-btn:hover { border-color: rgba(255,255,255,0.2); color: #fff; }
        .year-btn.active { background: var(--primary); border-color: var(--primary); color: #fff; box-shadow: 0 4px 12px rgba(99, 102, 241, 0.3); }
        .grid { display: flex; flex-direction: column; gap: 24px; }
        .grid:empty { min-height: 60vh; }
        .row { position: relative; display: flex; flex-direction: row; gap: 24px; padding: 24px; align-items: flex-start; background: linear-gradient(180deg, rgba(20,28,48,0.88), rgba(15,22,40,0.94)); border: 1px solid rgba(255,255,255,0.06); border-radius: 20px; box-shadow: 0 4px 20px rgba(0,0,0,0.15); transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1), border-color 0.3s, box-shadow 0.3s; overflow: hidden; z-index: 1; }
        .row::before { content: ""; position: absolute; top: 0; left: 0; right: 0; bottom: 0; background-image: var(--bg-thumb); background-size: 120%; background-position: center; filter: blur(40px) brightness(0.25) saturate(1.4); opacity: 0.55; transition: opacity 0.3s; z-index: -1; pointer-events: none; }
        .row * { position: relative; z-index: 2; }
        .row:hover { transform: translateY(-2px); border-color: rgba(255, 255, 255, 0.15); box-shadow: 0 12px 30px rgba(0,0,0,0.3); }
        .row:hover::before { opacity: 0.6; }
        .v-date { position: absolute; top: 24px; right: 24px; color: var(--text-muted); font-size: 13px; font-weight: 700; background: rgba(15, 23, 42, 0.6); padding: 6px 12px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.06); letter-spacing: 0.5px; z-index: 3; }
        .v-content-block { display: flex; flex-direction: column; gap: 16px; flex-grow: 1; padding-right: 110px; }
        .v-link { text-decoration: none; flex-shrink: 0; }
        .v-title-link { text-decoration: none; align-self: flex-start; }
        .v-title-link:hover .v-title { color: #fff; text-shadow: 0 0 10px rgba(255,255,255,0.1); }
        .img-container { position: relative; width: 160px; height: 90px; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.3); background: #151c2d; border: 1px solid rgba(255,255,255,0.05); transform: translateZ(0); }
        .img-container::before { content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: #1e293b; z-index: 0; }
        .img-container img { width: 101%; height: 100%; object-fit: cover; display: block; transition: transform 0.3s, opacity 0.2s; position: relative; z-index: 1; }
        .img-container:hover img { transform: scale(1.05); }
        .play-overlay { position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15, 23, 42, 0.75); color: #fff; font-size: 12px; display: flex; align-items: center; justify-content: center; opacity: 0; transition: opacity 0.2s; font-weight: bold; z-index: 2; }
        .img-container:hover .play-overlay { opacity: 1; }
        .v-title { font-size: 18px; color: #f8fafc; font-weight: 700; line-height: 1.4; transition: color 0.2s; overflow-wrap: break-word; }
        .v-tcs { width: 100%; max-width: 600px; }
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
        .player-link {
            margin-left: 20px;
            font-size: 16px;
            color: var(--primary);
            text-decoration: none;
            font-weight: 600;
            transition: color 0.2s;
        }
        .player-link:hover {
            color: #a5b4fc;
        }
        @media (max-width: 768px) { .header-flex { flex-direction: column; align-items: flex-start; gap: 14px; } .search-box { width: 100%; max-width: 100%; } .row { flex-direction: column; gap: 16px; padding: 20px; } .row::before { z-index: -1 !important; filter: blur(45px) saturate(0.9) !important; opacity: 0.5 !important; } .v-date { position: static; margin-bottom: 0; font-size: 12px; align-self: flex-start; padding: 4px 8px; z-index: 2; } .v-content-block { padding-right: 0; margin-top: 0; gap: 12px; z-index: 2; width: 100%; } .v-link { display: block; width: 100%; z-index: 2; } .img-container { width: 100%; height: auto; aspect-ratio: 16/9; border-radius: 14px; overflow: hidden; } .img-container img { z-index: 1 !important; } .play-overlay { display: none !important; } .v-title { font-size: 16px; } .v-tcs { max-width: 100%; width: 100%; } details { margin: 0 -20px; border-left: none; border-right: none; border-radius: 0; padding: 12px 20px; transition: none !important; } details[open] { border-bottom: none; background: rgba(15,23,42,0.8); } .no-tc-block { margin: 0 -20px; border-left: none; border-right: none; border-radius: 0; padding: 12px 20px; display: none; } .no-tc-block span { font-weight: 600; color: #cbd5e1; font-size: 14px; font-style: normal; } .tc-list { padding-left: 8px; padding-right: 8px; max-height: 220px; overflow-y: auto; scroll-behavior: auto; -webkit-overflow-scrolling: touch; } .t-click { margin-right: 6px; } .header-panel { position: relative; } summary { -webkit-tap-highlight-color: transparent; } }
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
            <label class="sponsor-toggle" id="sponsorToggleLabel" style="display:none;">
                <input type="checkbox" id="sponsorToggle" onchange="executeSearch(searchInput.value.toLowerCase().trim())">
                <span class="toggle-switch"></span>
                Показывать спонсорские видео
            </label>
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
        noteDiv.style.left = left + '%';
        noteDiv.style.top = top + '%';
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
            n.element.style.top = newTop + '%';
        }
        ticking = false;
    }
    window.addEventListener('scroll', function() {
        if (!ticking) {
            requestAnimationFrame(updateNotesPosition);
            ticking = true;
        }
    }, { passive: true });
    window.addEventListener('resize', function() {
        updateNotesPosition();
    });
    updateNotesPosition();
})();

function escapeHtml(str) {
    try {
        if (str == null) return '';
        const div = document.createElement('div');
        div.appendChild(document.createTextNode(String(str)));
        return div.innerHTML;
    } catch(e) {
        return String(str || '');
    }
}

function normalize(str) {
    try {
        if (str == null) return '';
        return String(str).toLowerCase()
                  .replace(/ë/g, 'e')
                  .replace(/[^a-zа-яё0-9]/g, '');
    } catch(e) {
        return '';
    }
}

function removeSpecificEmojis(str) {
    try {
        if (str == null) return '';
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
            return String(str).replace(/[\u{1F600}-\u{1F64F}\u{1F300}-\u{1F5FF}\u{1F680}-\u{1F6FF}\u{1F1E0}-\u{1F1FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{1F900}-\u{1F9FF}\u{1FA00}-\u{1FA6F}\u{1FA70}-\u{1FAFF}]/gu, '');
        }
    } catch(e) {
        return String(str || '');
    }
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
    "океанэлзи": ["океанэлзи", "элзи", "океанэльзы", "эльзы", "океанельзи", "ельзи"],
    "iowa": ["iowa", "айова"]
};

const variantToCanon = new Map();
for (const [canon, variants] of Object.entries(SYNONYMS)) {
    for (const v of variants) {
        variantToCanon.set(v, canon);
    }
}

function getSearchVariants(query) {
    try {
        if (!query) return [];
        const normQuery = normalize(query);
        if (variantToCanon.has(normQuery)) {
            const canon = variantToCanon.get(normQuery);
            return SYNONYMS[canon] || [normQuery];
        }
        return [normQuery];
    } catch(e) {
        return [normalize(query)];
    }
}

function matchesWithVariants(textNorm, variants) {
    try {
        if (!textNorm || !variants || variants.length === 0) return false;
        for (let v of variants) {
            const normVariant = normalize(v);
            if (textNorm.includes(normVariant)) return true;
        }
        return false;
    } catch(e) {
        return false;
    }
}

function highlightFirstMatch(escapedText, variants) {
    try {
        if (!variants || variants.length === 0 || !escapedText) return escapedText || '';
        const normEscaped = normalize(escapedText);
        let bestMatch = null;
        let bestIndex = Infinity;
        for (let v of variants) {
            const normV = normalize(v);
            const idx = normEscaped.indexOf(normV);
            if (idx !== -1 && idx < bestIndex) {
                bestIndex = idx;
                bestMatch = normV;
            }
        }
        if (bestMatch === null) return escapedText;
        let origIdx = 0, normIdx = 0;
        while (normIdx < bestIndex && origIdx < escapedText.length) {
            const ch = escapedText[origIdx];
            const nch = normalize(ch);
            if (nch.length > 0) normIdx++;
            origIdx++;
        }
        const start = origIdx;
        while (normIdx < bestIndex + bestMatch.length && origIdx < escapedText.length) {
            const ch = escapedText[origIdx];
            const nch = normalize(ch);
            if (nch.length > 0) normIdx++;
            origIdx++;
        }
        const end = origIdx;
        return escapedText.substring(0, start) + '<mark>' + escapedText.substring(start, end) + '</mark>' + escapedText.substring(end);
    } catch(e) {
        return escapedText || '';
    }
}

let streamsData = [];
let songsDB = {};
let searchIndex = [];
let activeYear = 'all';
let allRows = [];

function getTimecodeSeconds(line) {
    try {
        if (!line) return 99999999;
        const match = line.match(/(\d{1,2}:\d{2}(?::\d{2})?)/);
        if (!match) return 99999999;
        const parts = match[1].split(':').map(Number);
        if (parts.length === 2) return parts[0]*60 + parts[1];
        if (parts.length === 3) return parts[0]*3600 + parts[1]*60 + parts[2];
        return 99999999;
    } catch(e) {
        return 99999999;
    }
}

function normalizeTimecode(tc) {
    try {
        if (!tc) return '00:00:00';
        const parts = tc.split(':').map(Number);
        if (parts.length === 2) return `00:${parts[0].toString().padStart(2,'0')}:${parts[1].toString().padStart(2,'0')}`;
        if (parts.length === 3) return parts.map(p => p.toString().padStart(2,'0')).join(':');
        return tc;
    } catch(e) {
        return '00:00:00';
    }
}

async function loadDatabase() {
    if (streamsData.length > 0) return;
    try {
        const response = await fetch('parsed_streams_db.json');
        if (!response.ok) throw new Error('Файл базы не найден');
        let rawData = await response.json();
        rawData.sort((a, b) => {
            const dateA = a.published_date || '1970-01-01';
            const dateB = b.published_date || '1970-01-01';
            return dateB.localeCompare(dateA);
        });
        streamsData = rawData.filter(entry => entry.list_type === 'tracklist' || entry.list_type === 'mixed');
        songsDB = {};
        searchIndex = [];
        streamsData.forEach(entry => {
            const vId = entry.id;
            const timecodes = entry.timecodes || [];
            const sorted = timecodes.slice().sort((a,b) => getTimecodeSeconds(a) - getTimecodeSeconds(b));
            const tracks = sorted.map(line => {
                try {
                    const cleanedLine = String(line || '').replace(
                        /(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—]\s*\d{1,2}:\d{2}(?::\d{2})?/,
                        '$1'
                    );
                    const match = cleanedLine.match(/(\d{1,2}:\d{2}(?::\d{2})?)/);
                    if (match) {
                        let s = cleanedLine.replace(match[1], '').trim();
                        s = s.replace(/^[-–—]\s*/, '');
                        s = removeSpecificEmojis(s);
                        return { t: match[1], s: s };
                    } else {
                        let s = removeSpecificEmojis(cleanedLine);
                        return { s: s };
                    }
                } catch(e) {
                    return { s: '' };
                }
            });
            songsDB[vId] = { tracks, author: entry.author || '' };
            tracks.forEach(tr => {
                try {
                    const norm = normalize(tr.s || '');
                    searchIndex.push({ id: vId, text: tr.s, norm: norm });
                } catch(e) {}
            });
        });
    } catch(e) {
        console.error('Ошибка загрузки базы:', e);
        streamsData = [];
        const grid = document.getElementById('mainGrid');
        if (grid) {
            grid.innerHTML = '<div style="padding:40px;text-align:center;color:#94a3b8;">⚠️ Не удалось загрузить базу треклистов. Проверьте подключение к интернету.</div>';
        }
    }
}

function renderStreamHTML(stream) {
    try {
        const vId = stream.id;
        const title = escapeHtml(stream.title || 'Без названия');
        
        let date = 'Неизвестно';
        let rawYear = '0000';
        
        if (stream.published_date) {
            try {
                let dateStr = stream.published_date;
                if (typeof dateStr !== 'string') {
                    dateStr = String(dateStr);
                }
                const parts = dateStr.split('-');
                if (parts.length === 3) {
                    date = `${parts[2]}.${parts[1]}.${parts[0]}`;
                    rawYear = parts[0];
                }
            } catch(e) {
                date = 'Неизвестно';
                rawYear = '0000';
            }
        }
        date = escapeHtml(date);
        
        const timecodes = stream.timecodes || [];
        const listType = stream.list_type || 'none';
        const isSponsor = stream.is_sponsor || false;
        const hasTracks = timecodes.length > 0;
        const sponsorClass = isSponsor ? ' sponsor' : '';
        const badgeClass = `badge-${listType}`;
        const badgeText = listType === 'tracklist' ? 'Готовый трек-лист' : (listType === 'mixed' ? 'Сборный список' : '');
        const tcsHTML = hasTracks ? `<details><summary><div class="summary-flex"><span>Треклист ${timecodes.length}</span><span class="badge ${badgeClass}">${badgeText}</span></div></summary><div class="tc-list"></div></details>` : '<div class="no-tc-block"><span>Треклист не найден</span></div>';
        return `<div class="row${sponsorClass}" data-id="${vId}" data-year="${rawYear}" style="--bg-thumb: url('https://img.youtube.com/vi/${vId}/hqdefault.jpg');"><span class="v-date">${date}</span><a class="v-link" href="https://www.youtube.com/watch?v=${vId}" target="_blank"><div class="img-container"><img loading="lazy" decoding="async" src="https://img.youtube.com/vi/${vId}/hqdefault.jpg" alt="" onerror="this.style.opacity='0';"><span class="play-overlay">▶ Смотреть</span></div></a><div class="v-content-block"><a class="v-title-link" href="https://www.youtube.com/watch?v=${vId}" target="_blank"><span class="v-title">${title}</span></a><div class="v-tcs">${tcsHTML}</div></div></div>`;
    } catch(e) {
        return '<div class="row" style="padding:20px;color:#94a3b8;">Ошибка рендеринга</div>';
    }
}

function animateContainer(container) {
    if (!container) return;
    try {
        container.style.transition = 'none';
        container.style.opacity = '0';
        container.style.transform = 'translateY(-10px)';
        void container.offsetHeight;
        container.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
        container.style.opacity = '1';
        container.style.transform = 'translateY(0)';
    } catch(e) {}
}

function renderAllStreams() {
    try {
        const grid = document.getElementById('mainGrid');
        grid.innerHTML = streamsData.map(renderStreamHTML).join('');
        allRows = [...grid.querySelectorAll('.row')];
        document.querySelectorAll('details').forEach(details => {
            details.addEventListener('toggle', function() {
                try {
                    const container = this.querySelector('.tc-list');
                    if (!container) return;
                    if (this.open) {
                        const row = this.closest('.row');
                        const vId = row ? row.getAttribute('data-id') : null;
                        const filter = searchInput.value.toLowerCase().trim();
                        if (container.children.length === 0 && vId) renderTracklist(vId, container, filter);
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
                } catch(e) {}
            });
        });
    } catch(e) {
        console.error('Ошибка рендеринга:', e);
    }
}

function initYearFilters() {
    try {
        const yearsSet = new Set();
        allRows.forEach(row => { const y = row.getAttribute('data-year'); if(y && y !== '0000') yearsSet.add(y); });
        const sortedYears = Array.from(yearsSet).sort().reverse();
        const container = document.getElementById('yearFilters');
        const currentYear = new Date().getFullYear().toString();
        
        let html = '<button class="year-btn" data-year="all">Все годы</button>';
        sortedYears.forEach(y => {
            const isActive = y === currentYear ? ' active' : '';
            html += `<button class="year-btn${isActive}" data-year="${y}">${y} года</button>`;
        });
        container.innerHTML = html;
        
        if (sortedYears.includes(currentYear)) {
            activeYear = currentYear;
        } else {
            activeYear = 'all';
            const allBtn = container.querySelector('[data-year="all"]');
            if (allBtn) allBtn.classList.add('active');
        }
        
        container.addEventListener('click', (e) => {
            if(e.target.classList.contains('year-btn')) {
                document.querySelectorAll('.year-btn').forEach(b => b.classList.remove('active'));
                e.target.classList.add('active');
                activeYear = e.target.getAttribute('data-year');
                executeSearch(searchInput.value.toLowerCase().trim());
            }
        });
    } catch(e) {
        console.error('Ошибка инициализации фильтров:', e);
    }
}

function renderTracklist(vId, container, filter) {
    try {
        const tracks = songsDB[vId]?.tracks || [];
        let html = '';
        if (filter) {
            const variants = getSearchVariants(filter);
            tracks.forEach(tr => {
                try {
                    let sText = tr.s || '';
                    sText = escapeHtml(sText);
                    const normText = normalize(sText);
                    if (matchesWithVariants(normText, variants)) {
                        sText = highlightFirstMatch(sText, variants);
                    }
                    const displayedTime = tr.t ? normalizeTimecode(tr.t) : '';
                    html += tr.t ? `<div class="tc-item"><span class="t-click" data-time="${tr.t}">${displayedTime}</span><span class="s-title">${sText}</span></div>` : `<div class="tc-item"><span class="s-title">${sText}</span></div>`;
                } catch(e) {}
            });
        } else {
            tracks.forEach(tr => {
                try {
                    const displayedTime = tr.t ? normalizeTimecode(tr.t) : '';
                    const safeText = escapeHtml(tr.s || '');
                    html += tr.t ? `<div class="tc-item"><span class="t-click" data-time="${tr.t}">${displayedTime}</span><span class="s-title">${safeText}</span></div>` : `<div class="tc-item"><span class="s-title">${safeText}</span></div>`;
                } catch(e) {}
            });
        }
        const author = songsDB[vId]?.author;
        if (author && author.trim() !== '') html += `<div class="tc-author">Автор треклиста: ${escapeHtml(author)}</div>`;
        container.innerHTML = html;
    } catch(e) {
        container.innerHTML = '<div class="tc-item">Ошибка загрузки треков</div>';
    }
}

const searchInput = document.getElementById('sInput');
const clearBtn = document.getElementById('sClear');
const statsEl = document.getElementById('searchStats');

async function executeSearch(filter) {
    try {
        if (allRows.length === 0) return;
        let visibleCount = 0;
        const sponsorToggle = document.getElementById('sponsorToggle');
        const showSponsors = sponsorToggle ? sponsorToggle.checked : false;
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
            allRows.forEach(row => { 
                const rowYear = row.getAttribute('data-year'); 
                const isSponsor = row.classList.contains('sponsor');
                if ((activeYear === 'all' || rowYear === activeYear) && (showSponsors || !isSponsor)) { 
                    row.style.display = ''; 
                    visibleCount++; 
                } 
            });
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
            const isSponsor = row.classList.contains('sponsor');
            if (!showSponsors && isSponsor) return;
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
    } catch(e) {
        console.error('Ошибка поиска:', e);
    }
}

const scrollBtn = document.getElementById('scrollTopBtn');
window.addEventListener('scroll', function() {
    try {
        if (window.scrollY > 300) {
            if (scrollBtn) scrollBtn.classList.add('show');
        } else {
            if (scrollBtn) scrollBtn.classList.remove('show');
        }
    } catch(e) {}
}, { passive: true });

if (scrollBtn) {
    scrollBtn.addEventListener('click', function() {
        try {
            window.scrollTo({ top: 0, behavior: 'smooth' });
        } catch(e) {}
    });
}

document.addEventListener('DOMContentLoaded', async () => {
    try {
        await loadDatabase();
        renderAllStreams();
        initYearFilters();
        
        const sponsorToggleLabel = document.getElementById('sponsorToggleLabel');
        if (sponsorToggleLabel && document.querySelector('.row.sponsor')) {
            sponsorToggleLabel.style.display = 'flex';
        }
        
        executeSearch('');
    } catch(e) {
        console.error('Ошибка инициализации:', e);
    }
});

document.getElementById('mainGrid').addEventListener('click', (e) => {
    try {
        if (e.target.classList.contains('t-click')) {
            e.preventDefault();
            const time = e.target.getAttribute('data-time');
            const vId = e.target.closest('.row').getAttribute('data-id');
            const parts = time.split(':').map(Number);
            const secs = parts.length === 2 ? parts[0]*60 + parts[1] : parts[0]*3600 + parts[1]*60 + parts[2];
            window.open(`https://www.youtube.com/watch?v=${vId}&t=${secs}s`, '_blank');
        }
    } catch(e) {}
});

let debounceTimer;
searchInput.addEventListener('input', function() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function() {
        try {
            executeSearch(searchInput.value.toLowerCase().trim());
        } catch(e) {}
    }, 700);
});

clearBtn.addEventListener('click', function() {
    searchInput.value = '';
    clearBtn.style.display = 'none';
    executeSearch('');
});
</script>
</body>
</html>"""

if __name__ == "__main__":
    run_parser()