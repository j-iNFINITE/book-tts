#!/usr/bin/env python3
"""
独立 TTS 有声书生成器 - 支持单本或批量处理文件夹内的 EPUB

用法:
    python tts_audiobook.py <epub文件或目录> [--output <输出根目录>] [--voice <音色>] [--style <风格>]

批量处理时，会按文件名顺序处理目录中所有 .epub 文件，每本书生成各自的工作子目录。
已完成的书（工作目录下存在 _COMPLETED 文件）会被自动跳过，中断后重新运行会继续未完成的书。
"""

import os
import sys
import re
import json
import base64
import logging
import shutil
import time
from pathlib import Path
from typing import List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET
import mobi
import shutil
import tempfile
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import numpy as np
import soundfile as sf
from pydub import AudioSegment
from tqdm import tqdm
from ebooklib import epub, ITEM_DOCUMENT
from bs4 import BeautifulSoup, Tag
import json_repair
import threading,random
os.environ["PYTHONUTF8"] = "1"

# ============================
# 配置区（可通过命令行参数覆盖）
# ============================
DEFAULT_OUTPUT_DIR = "audiobook_output"         # 默认输出根目录
DEFAULT_VOICE = "冰糖"                          # MiMo TTS 音色
DEFAULT_STYLE='''
【角色】 一位冷静克制的朗读者，像深夜独自翻阅书页时的内心声音。语调里没有表演欲，只有对文字的准确理解与转述。
【场景】 深夜书桌前，一灯如豆。这本书摊开在桌上，你闭目静听。不是人在对你说话，而是文字本身在脑中流动——干净的、没有杂音的输入。
【指导】 气息稳在胸腔，咬字清晰干净，每个字的边界分明而不锐利。中低音，语速中等均匀，段落间停顿稍长，句间停顿紧凑，不撒气声不打舌响。整体语调克制冷淡，没有刻意上扬的暖意，没有刻意下压的悲悯。只是在读——像翻页的手指没有多余动作，像目光扫过铅字没有额外的表情。用最朴素的传达，让文字自己说话。
'''
AUDIO_FORMAT = "mp3"                            # 最终音频格式
MAX_TTS_CHARS = 500                             # 单次 TTS 最长字符数（超长段落会切分）
DEFAULT_PARA_PAUSE_MS = 300                     # 段落间停顿毫秒数
COMPLETED_MARKER = "_COMPLETED"                 # 整书完成标记文件名
MAX_WORKERS = 10                                 # 并发合成线程数
DEBUG = True                    # 调试模式，记录每次 TTS 请求内容
# ============================
# MiMo TTS 客户端（带智能重试）
# ============================
class RateLimiter:
    """简单的请求频率限制器（滑动时间窗口）"""
    def __init__(self, max_calls: int, period: float = 60.0):
        self.max_calls = max_calls          # 周期内最大请求数
        self.period = period                # 周期长度（秒）
        self.calls = []                     # 记录请求时间戳
        self.lock = threading.Lock()

    def acquire(self):
        """阻塞直到获得请求许可"""
        with self.lock:
            now = time.monotonic()
            # 清除周期外的时间戳
            self.calls = [t for t in self.calls if now - t < self.period]

            if len(self.calls) >= self.max_calls:
                # 需要等待最早记录过期
                sleep_time = self.calls[0] + self.period - now
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    now = time.monotonic()
                    # 重新清理
                    self.calls = [t for t in self.calls if now - t < self.period]

            self.calls.append(now)
class MiMoTTSClient:
    """封装 MiMo V2.5 TTS HTTP 客户端，支持多 API key 轮换重试"""

    def __init__(self, api_keys: List[str], base_url: str = "https://api.xiaomimimo.com/v1",
                 rpm_limit: int = 90):
        if not api_keys:
            raise ValueError("至少需要提供一个 API key")
        self.api_keys = api_keys
        self.base_url = base_url

        # 创建带重试策略的 session（只对瞬时网络错误重试，与 key 轮换无关）
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # 全局速率限制器（多 key 共享，若需要按 key 独立限速可后续优化）
        self.rate_limiter = RateLimiter(max_calls=rpm_limit, period=60.0)

    def synthesize(self, text: str, voice: str = "冰糖", style: str = "",
                   audio_format: str = "wav") -> bytes:
        """合成语音，失败时自动轮换 API key 重试"""
        self.rate_limiter.acquire()

        messages = []
        if style:
            messages.append({"role": "user", "content": style})
        messages.append({"role": "assistant", "content": text})

        payload = {
            "model": "mimo-v2.5-tts",
            "messages": messages,
            "audio": {"format": audio_format, "voice": voice}
        }
        if DEBUG:
            logging.debug(f"TTS请求 payload: {json.dumps(payload, ensure_ascii=False)}")

        # 随机选择起始 key，避免所有并发线程同时抢同一个 key
        start_idx = random.randint(0, len(self.api_keys) - 1)
        last_exception = None

        # 尝试所有 key（每个线程独立轮换，无锁）
        for offset in range(len(self.api_keys)):
            key = self.api_keys[(start_idx + offset) % len(self.api_keys)]
            headers = {
                "Content-Type": "application/json",
                "api-key": key
            }
            try:
                resp = self.session.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=60         # 一分钟超时，触发异常进入 next key
                )
                resp.raise_for_status()
                data = resp.json()
                audio_b64 = data["choices"][0]["message"]["audio"]["data"]
                return base64.b64decode(audio_b64)   # 成功：直接返回
            except Exception as e:
                masked_key = key[:8] + "..." if len(key) > 8 else key
                logging.warning(f"API key {masked_key} 请求失败: {e}")
                last_exception = e
                # 失败则自动尝试下一个 key（循环 continue）

        # 所有 key 均失败
        raise RuntimeError("所有 API key 均尝试失败") from last_exception

# ============================
# 通用工具函数
# ============================
def clean_text_for_tts(text: str) -> str:
    """
    预处理文本，移除或替换可能被 MiMo TTS 误识别为音频标签的内容。
    
    所有处理都是基于“宁滥勿缺”的安全策略，以防止句子被吞音。
    """
    if not text:
        return ""
    
    # 安全移除所有半角/全角圆括号及其内部内容
    text = re.sub(r'\([^\)]*\)', '', text)
    text = re.sub(r'（[^）]*）', '', text)
    
    # 安全移除所有半角方括号及其内部内容
    text = re.sub(r'\[[^\]]*\]', '', text)
    
    # 将残留的不成对单独括号替换为空格
    text = text.replace('(', ' ').replace(')', ' ')
    text = text.replace('（', ' ').replace('）', ' ')
    text = text.replace('[', ' ').replace(']', ' ')
    
    # 合并多余空白字符
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def sanitize_filename(name: str) -> str:
    """移除文件名中的非法字符，限制长度"""
    name = re.sub(r'[\\/*?:"<>|]', "", name).strip().replace(" ", "_")
    return name[:80]


def safe_json_load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json_repair.load(f)
    except Exception:
        return {}


def safe_json_save(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        raise EnvironmentError(
            "未找到 ffmpeg。请安装 ffmpeg 并将其添加到 PATH。\n"
            "下载: https://ffmpeg.org/download.html"
        )


def extract_original_paragraphs(soup: BeautifulSoup) -> List[str]:
    """从 BeautifulSoup 中提取所有正文段落（原文），避免重复与截断"""
    block_tags = ["p", "div", "h1", "h2", "h3", "h4"]
    elements = []
    
    for tag in soup.find_all(block_tags):
        # 如果是容器（内部还有块级标签），跳过，避免重复提取
        if tag.find(block_tags):
            continue
        
        text = tag.get_text(strip=True)
        if not text or len(text) < 3:
            continue
        
        cls = " ".join(tag.get("class", [])).lower()
        if "toc" in cls or "note" in cls:
            continue
        
        # 标题由 split_html_by_headings 统一处理，这里跳过
        if tag.name in ("h1", "h2", "h3", "h4"):
            continue
        
        elements.append(text)
    
    return elements
def extract_original_paragraphs_old(soup: BeautifulSoup) -> List[str]:
    """从 BeautifulSoup 中提取所有正文段落（原文）"""
    elements = []
    for tag in soup.find_all(["p", "div", "span", "h1", "h2", "h3", "h4"]):
        text = tag.get_text(strip=True)
        if not text or len(text) < 3:
            continue
        cls = " ".join(tag.get("class", [])).lower()
        if "toc" in cls or "note" in cls:
            continue
        if tag.name in ("h1", "h2", "h3", "h4"):
            continue
        elements.append(text)
    return elements
def parse_ncx(ncx_path: str) -> List[Tuple[str, str]]:
    """
    从 toc.ncx 中提取章节列表，返回 [(标题, 锚点id), ...]
    锚点 id 形如 'point_001', 'chapter_1' 等（不含 # 号）
    """
    ns = {'ncx': 'http://www.daisy.org/z3986/2005/ncx/'}
    tree = ET.parse(ncx_path)
    root = tree.getroot()
    chapters = []
    # 查找所有 navPoint
    for navpoint in root.findall('.//ncx:navPoint', ns):
        label_elem = navpoint.find('ncx:navLabel/ncx:text', ns)
        title = label_elem.text if label_elem is not None else "未命名章节"
        content = navpoint.find('ncx:content', ns)
        src = content.get('src') if content is not None else ''
        # src 格式通常是 "book.html#point_001"
        if '#' in src:
            anchor = src.split('#')[-1]
        else:
            anchor = None
        chapters.append((title, anchor))
    return chapters
def extract_chapter_soup(html_soup: BeautifulSoup, anchor_id: str, next_anchor_id: str = None) -> BeautifulSoup:
    """
    从整个 book.html 的 soup 中切出从 anchor_id 所在位置开始，
    到下一个锚点（next_anchor_id）之前的内容。
    如果没有下一个锚点，则提取到文档结尾。
    返回一个新的 BeautifulSoup 对象，仅包含该章节的内容。
    """
    # 定位起始标签
    start_tag = html_soup.find(id=anchor_id) if anchor_id else html_soup.find('body')
    if start_tag is None:
        # 如果找不到指定 id，就返回整个文档
        return html_soup

    # 定位结束位置（下一个锚点的标签）
    end_tag = None
    if next_anchor_id:
        end_tag = html_soup.find(id=next_anchor_id)

    # 收集从 start_tag 开始到 end_tag 之前的所有元素
    from itertools import takewhile
    # 遍历文档中所有元素，复杂的做法：使用元素的索引位置
    # 简便方法：将 start_tag 及其后续兄弟元素装进新 soup
    new_soup = BeautifulSoup('<div class="chapter-wrapper"></div>', 'html.parser')
    wrapper = new_soup.div
    current = start_tag
    while current:
        if end_tag and current == end_tag:
            break
        # 复制当前标签及其内容（深拷贝）
        wrapper.append(current.__copy__())
        # 移动到下一个兄弟元素
        current = current.find_next_sibling()
        if current is None:
            # 如果当前元素没有兄弟，尝试找父级的下一个兄弟？一般不必要
            break

    # 如果 wrapper 里没有任何内容，则返回整个原始 soup（兜底）
    if not wrapper.contents:
        return html_soup
    return new_soup
def split_html_by_headings(soup: BeautifulSoup, min_paragraphs: int = 3) -> List[Tuple[str, List]]:
    """
    将全书 HTML 按标题拆分成多个逻辑章节 (标题, [标签列表]).
    只计数包含实际文本的非标题元素，避免空 div 等干扰。
    """
    body = soup.find('body')
    if not body:
        return []

    sections = []
    current_title = ""
    current_tags = []
    non_heading_count = 0

    def is_meaningful_tag(tag: Tag) -> bool:
        """判断一个标签是否包含可朗读的文本（非标题且非空白）"""
        if tag.name not in ('div', 'p', 'span', 'section', 'article'):
            return False
        text = tag.get_text(strip=True)
        return len(text) > 0

    for child in body.children:
        if isinstance(child, str):
            if child.strip() == '':
                continue
            continue  # 忽略非空文本节点，EPUB中极少出现

        if isinstance(child, Tag):
            if child.name in ('h1', 'h2', 'h3', 'h4'):
                title_text = child.get_text(strip=True)
                if not title_text:
                    # 标题为空，作为普通内容处理
                    current_tags.append(child)
                    if is_meaningful_tag(child):
                        non_heading_count += 1
                    continue

                # 如果累积的内容已经足够，保存当前章节
                if current_tags and non_heading_count >= min_paragraphs:
                    sections.append((current_title, current_tags))
                    current_tags = []
                    non_heading_count = 0

                # 更新标题
                current_title = title_text
                current_tags.append(child)

            else:
                current_tags.append(child)
                if is_meaningful_tag(child):
                    non_heading_count += 1
        else:
            current_tags.append(child)

    if current_tags:
        sections.append((current_title, current_tags))

    if not sections:
        return [("完整内容", list(body.children))]
    return sections


def get_skip_items(book: epub.EpubBook) -> set:
    """获取应跳过的文件名称集合（封面、目录、版权页等）"""
    skip_set = set()
    skip_guide_types = {'title-page', 'toc', 'copyright-page', 'acknowledgements', 'foreword'}
    
    # 基于 EPUB guide 的跳过
    try:
        for ref in book.guide:
            ref_type = ref.get('type', '')
            if ref_type in skip_guide_types:
                href = ref.get('href', '')
                item = book.get_item_with_href(href)
                if item:
                    skip_set.add(item.get_name())
    except Exception as e:
        logging.warning(f"解析 EPUB guide 失败: {e}")

    # 基于文件名的跳过
    name_keywords = ['cover', 'toc', 'colophon', 'copyright', 'imprint']
    for item in book.get_items():
        name = os.path.basename(item.get_name()).lower()
        if any(kw in name for kw in name_keywords):
            skip_set.add(item.get_name())

    # 基于内容的版权页检测
    for item in book.get_items():
        if item.get_name() in skip_set or item.get_type() != ITEM_DOCUMENT:
            continue
        try:
            content = item.get_content().decode('utf-8', errors='ignore')
            soup = BeautifulSoup(content, 'html.parser')
            if soup.find(class_='kindle-cn-copyright-text'):
                skip_set.add(item.get_name())
                logging.debug(f"基于内容版权类跳过: {item.get_name()}")
                continue
            if soup.find(string=re.compile(r'图书在版编目')):
                skip_set.add(item.get_name())
                logging.debug(f"基于CIP文字跳过: {item.get_name()}")
                continue
        except Exception:
            continue
    # 基于文本纯净度的跳过（仅图片/装饰页）
    MIN_VISIBLE_CHARS = 30  # 可调阈值，小于此长度的页面视为非正文
    for item in book.get_items():
        if item.get_name() in skip_set or item.get_type() != ITEM_DOCUMENT:
            continue
        try:
            content = item.get_content().decode('utf-8', errors='ignore')
            soup = BeautifulSoup(content, 'html.parser')
            visible_text = soup.get_text(separator=' ', strip=True)
            if len(visible_text) < MIN_VISIBLE_CHARS:
                skip_set.add(item.get_name())
                logging.debug(f"基于文本量不足跳过 ({len(visible_text)}字符): {item.get_name()}")
        except Exception:
            continue
    return skip_set


def split_long_paragraph(text: str, max_chars: int = MAX_TTS_CHARS) -> List[str]:
    """将超长段落按句号等切分，保证每段不超过 max_chars"""
    if len(text) <= max_chars:
        return [text]
    chunks = []
    sentences = re.split(r'(?<=[。！？])', text)
    cur = ""
    for s in sentences:
        if len(cur) + len(s) > max_chars and cur:
            chunks.append(cur)
            cur = s
        else:
            cur += s
    if cur:
        chunks.append(cur)
    return chunks


def synthesize_paragraph(paragraph: str, para_idx: int,
                         audio_dir: Path, tts_client: MiMoTTSClient,
                         voice: str, style: str) -> List[Path]:
    """为单个段落合成 WAV 文件，可能切分，返回文件路径列表"""
    paragraph = clean_text_for_tts(paragraph)
    sub_texts = split_long_paragraph(paragraph)
    paths = []
    for sub_idx, sub in enumerate(sub_texts):
        if not sub.strip():
            continue
        fname = f"para_{para_idx:04d}_{sub_idx:02d}.wav"
        fpath = audio_dir / fname
        if fpath.exists():
            paths.append(fpath)
            continue
        try:
            audio_bytes = tts_client.synthesize(text=sub, voice=voice,
                                                style=style, audio_format="wav")
            with open(fpath, "wb") as f:
                f.write(audio_bytes)
            paths.append(fpath)
            logging.debug(f"  生成片段: {fpath.name}")
        except Exception as e:
            logging.error(f"TTS 失败: {fpath.name} - {e}")
    return paths
def synthesize_paragraph_with_retry(paragraph, para_idx, audio_dir, tts_client, voice, style, max_retries=3):
    for attempt in range(max_retries):
        try:
            return synthesize_paragraph(paragraph, para_idx, audio_dir, tts_client, voice, style)
        except Exception as e:
            logging.warning(f"段落 {para_idx} 第 {attempt+1} 次尝试失败: {e}")
            time.sleep(2 ** attempt)  # 指数退避
    logging.error(f"段落 {para_idx} 重试 {max_retries} 次后仍然失败，已跳过，内容：{paragraph}")
    return []

def merge_paragraphs_to_chapter(audio_dir: Path, safe_title: str,
                                output_dir: Path, fmt: str = "mp3",
                                pause_ms: int = DEFAULT_PARA_PAUSE_MS) -> Optional[Path]:
    """合并段落 WAV 为章节音频文件"""
    wavs = sorted(audio_dir.glob("para_*.wav"),
                  key=lambda p: int(p.stem.split("_")[1]))
    if not wavs:
        return None
    combined = AudioSegment.empty()
    silence = AudioSegment.silent(duration=pause_ms)
    for wf in wavs:
        try:
            seg = AudioSegment.from_file(str(wf), format="wav")
            combined += seg + silence
        except Exception as e:
            logging.warning(f"跳过损坏的音频片段 {wf.name}: {e}")
    if len(combined) == 0:
        return None
    out_path = output_dir / f"{safe_title}.{fmt}"
    export_kwargs = {"format": fmt}
    if fmt == "mp3":
        export_kwargs["bitrate"] = "192k"
    combined.export(str(out_path), **export_kwargs)
    logging.info(f"章节音频已生成: {out_path} ({len(combined)/1000:.1f}s)")
    return out_path
from ebooklib import epub as epub_reader
from PIL import Image
import io

def extract_epub_metadata(book: epub.EpubBook, work_dir: str):
    """提取 EPUB 元数据并保存 podcast.json 与封面"""
    # 获取标题（优先 dc:title）
    title = book.get_metadata('DC', 'title')
    title_text = title[0][0] if title else Path(work_dir).name

    # 获取作者
    creator = book.get_metadata('DC', 'creator')
    author_text = creator[0][0] if creator else "未知作者"

    # 获取描述
    description = book.get_metadata('DC', 'description')
    desc_text = description[0][0] if description else f"{title_text} - 有声书"

    # 获取语言
    language = book.get_metadata('DC', 'language')
    lang_text = language[0][0] if language else "zh-CN"

    # 构建 podcast.json
    podcast_meta = {
        "title": title_text,
        "author": author_text,
        "description": desc_text,
        "language": lang_text,
        "explicit": False,
        "category": "Arts/Books"
    }
    podcast_path = os.path.join(work_dir, "podcast.json")
    with open(podcast_path, "w", encoding="utf-8") as f:
        json.dump(podcast_meta, f, ensure_ascii=False, indent=2)
    logging.info(f"播客元数据保存至: {podcast_path}")

    # 提取封面图片
    try:
        cover_image = None
        # 方法1：从 guide 中获取封面项
        for guide_ref in book.guide:
            if guide_ref.get("type") == "cover":
                href = guide_ref.get("href")
                item = book.get_item_with_href(href)
                if item:
                    cover_image = item
                    break
        # 方法2：遍历 items 找 image/jpeg, image/png 类型
        if not cover_image:
            for item in book.get_items():
                if item.get_type() in (epub_reader.ITEM_IMAGE, epub_reader.ITEM_COVER):
                    # 先取第一张图片，可能有多种分辨率的封面，通常取较大的
                    if cover_image is None or len(item.get_content()) > len(cover_image.get_content()):
                        cover_image = item
        if cover_image:
            img_data = cover_image.get_content()
            img = Image.open(io.BytesIO(img_data))
            # 统一转为 JPEG 并保存
            cover_path = os.path.join(work_dir, "cover.jpg")
            if img.mode == "RGBA":
                img = img.convert("RGB")
            img.save(cover_path, "JPEG", quality=85)
            logging.info(f"封面保存至: {cover_path}")
        else:
            logging.warning("未找到封面图片")
    except Exception as e:
        logging.warning(f"封面提取失败: {e}")
def parse_opf_metadata(opf_path: str) -> dict:
    """
    从 content.opf 中提取元数据，返回字典。
    """
    ns = {
        'dc': 'http://purl.org/dc/elements/1.1/',
        'opf': 'http://www.idpf.org/2007/opf'
    }
    tree = ET.parse(opf_path)
    root = tree.getroot()

    metadata = {}

    # 标题
    title_elem = root.find('.//dc:title', ns)
    metadata['title'] = title_elem.text if title_elem is not None else "未知标题"

    # 作者（可能有多个，取第一个）
    creator_elem = root.find('.//dc:creator', ns)
    metadata['author'] = creator_elem.text if creator_elem is not None else "未知作者"

    # 语言
    language_elem = root.find('.//dc:language', ns)
    metadata['language'] = language_elem.text if language_elem is not None else "zh-CN"

    # 描述
    description_elem = root.find('.//dc:description', ns)
    metadata['description'] = description_elem.text if description_elem is not None else ""

    # 出版商（可选）
    publisher_elem = root.find('.//dc:publisher', ns)
    metadata['publisher'] = publisher_elem.text if publisher_elem is not None else ""

    # 出版日期
    date_elem = root.find('.//dc:date', ns)
    metadata['date'] = date_elem.text if date_elem is not None else ""

    return metadata
# ============================
# 进度管理（用于单本书内的章节级别断点）
# ============================
PROGRESS_FILE = "tts_progress.json"

def load_book_progress(work_dir: str) -> dict:
    data = safe_json_load(os.path.join(work_dir, PROGRESS_FILE))
    if isinstance(data, int):  # 兼容旧格式
        return {"file_index": data, "section_index": 0}
    return data if isinstance(data, dict) else {"file_index": 0, "section_index": 0}


def save_book_progress(work_dir: str, progress: dict):
    safe_json_save(os.path.join(work_dir, PROGRESS_FILE), {
        "file_index": progress["file_index"],
        "section_index": progress["section_index"]
    })


# ============================
# 处理单本书的核心流程
# ============================
def process_epub(epub_path: str, output_base: str = DEFAULT_OUTPUT_DIR,
                 voice: str = DEFAULT_VOICE, style: str = DEFAULT_STYLE,
                 tts_api_keys: List[str] = None):
    if tts_api_keys is None:
        tts_api_keys = os.environ.get("MIMO_TTS_API_KEYS", "").split(",")
        tts_api_keys = [k.strip() for k in tts_api_keys if k.strip()]
    if not tts_api_keys:
        raise ValueError("请提供至少一个 API key")
    
    tts_client = MiMoTTSClient(api_keys=tts_api_keys, rpm_limit=95)

    book_name = Path(epub_path).stem
    work_dir = os.path.join(output_base, book_name)
    os.makedirs(work_dir, exist_ok=True)

    # 如果整书已完成，直接跳过
    completed_marker = os.path.join(work_dir, COMPLETED_MARKER)
    if os.path.exists(completed_marker):
        logging.info(f"书籍 {book_name} 已完成，跳过。")
        print(f"【跳过】{book_name}（已完成）")
        return

    log_file = os.path.join(work_dir, "tts.log")

    # 获取根日志记录器，移除已有的所有 handlers
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # 创建文件处理器，不使用控制台处理器
    file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

    root_logger.addHandler(file_handler)
    root_logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

    logging.info(f"开始处理: {epub_path}")

    # 解包 EPUB
    book = epub.read_epub(epub_path)

    # 准备文档项目（按 spine 顺序）
    doc_items = []
    for item_id, _ in book.spine:
        item = book.get_item_with_id(item_id)
        if item and item.get_type() == ITEM_DOCUMENT:
            doc_items.append(item)

    skip_items = get_skip_items(book)
    audiobook_dir = os.path.join(work_dir, "mp3")
    os.makedirs(audiobook_dir, exist_ok=True)

    # 加载章节级进度
    progress = load_book_progress(work_dir)
    start_file = progress.get("file_index", 0)
    start_section = progress.get("section_index", 0)
    logging.info(f"从断点恢复：文件{start_file}, 小节{start_section}")

    # 创建线程池，用于章节内段落并发合成
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

    # 遍历文档
    for file_idx in range(start_file, len(doc_items)):
        item = doc_items[file_idx]
        item_name = item.get_name()

        if item_name in skip_items:
            logging.info(f"跳过非正文文件: {item_name}")
            if file_idx == start_file:
                save_book_progress(work_dir, {"file_index": file_idx + 1, "section_index": 0})
            continue

        # 读取 HTML 内容
        html_content = item.get_content().decode("utf-8")
        soup = BeautifulSoup(html_content, "html.parser")

        # 拆分章节
        sections = split_html_by_headings(soup)
        if not sections:
            continue

        if file_idx == start_file:
            sec_range = range(start_section, len(sections))
        else:
            sec_range = range(len(sections))

        for sec_idx in sec_range:
            title, tags = sections[sec_idx]

            # 构建临时 soup 来提取段落
            temp_soup = BeautifulSoup("", "html.parser")
            for tag in tags:
                temp_soup.append(tag)
            paragraphs = extract_original_paragraphs(temp_soup)

            # 清理标题并添加至段落列表最前面（作为第一句朗读）
            clean_title = re.sub(r'\s+', ' ', title).strip()
            if clean_title:
                if not paragraphs or paragraphs[0].strip() != clean_title:
                    paragraphs.insert(0, clean_title)
            if not paragraphs:
                continue

            # 生成带序号的文件名
            global_chapter_num = file_idx * 1000 + sec_idx + 1
            safe_title = f"{global_chapter_num:04d}_{sanitize_filename(title) or f'Section_{sec_idx+1}'}"
            output_mp3 = Path(audiobook_dir) / f"{safe_title}.{AUDIO_FORMAT}"

            if output_mp3.exists():
                logging.info(f"已存在，跳过: {output_mp3.name}")
                save_book_progress(work_dir, {"file_index": file_idx, "section_index": sec_idx + 1})
                continue

            logging.info(f"合成章节: {safe_title}")
            temp_dir = Path(work_dir) / f"temp_{file_idx:04d}_{sec_idx:04d}"
            temp_dir.mkdir(parents=True, exist_ok=True)

            # 使用线程池并发合成所有段落
            futures = {}
            for pi, para in enumerate(paragraphs):
                future = executor.submit(
                    synthesize_paragraph_with_retry, para, pi, temp_dir, tts_client, voice, style
                )
                futures[future] = pi

            # 显示进度并等待完成
            for future in tqdm(as_completed(futures), total=len(paragraphs),
                               desc=f"章节 {global_chapter_num}"):
                pi = futures[future]
                try:
                    _ = future.result()
                except Exception as e:
                    logging.error(f"段落 {pi} 合成最终失败: {e}")

            # 合并音频
            merge_paragraphs_to_chapter(temp_dir, safe_title, Path(audiobook_dir), AUDIO_FORMAT)
            shutil.rmtree(temp_dir, ignore_errors=True)

            # 保存进度
            save_book_progress(work_dir, {"file_index": file_idx, "section_index": sec_idx + 1})

        # 文件处理完全，重置 section 索引
        save_book_progress(work_dir, {"file_index": file_idx + 1, "section_index": 0})

    # 关闭线程池
    executor.shutdown(wait=True)
    # 生成播客元数据
    extract_epub_metadata(book, work_dir)
    # 全部章节完成，写入完成标记
    Path(completed_marker).touch()
    logging.info(f"书籍 {book_name} 处理完毕！")
    print(f"【完成】{book_name}")
def process_mobi(mobi_path: str, output_base: str = DEFAULT_OUTPUT_DIR,
                 voice: str = DEFAULT_VOICE, style: str = DEFAULT_STYLE,
                 tts_api_key: str = None):
    """
    处理 .mobi 文件：
      1. 使用 mobi 库解包到临时目录
      2. 读取 mobi7/book.html 和 toc.ncx
      3. 按章节提取 HTML，对每个章节合成一个音频文件
    """
    if tts_api_keys is None:
        # 从环境变量获取
        tts_api_keys = os.environ.get("MIMO_TTS_API_KEYS", "").split(",")
        tts_api_keys = [k.strip() for k in tts_api_keys if k.strip()]
    if not tts_api_keys:
        raise ValueError("请提供至少一个 API key (通过参数或环境变量 MIMO_TTS_API_KEYS)")

    check_ffmpeg()
    tts_client = MiMoTTSClient(api_keys=tts_api_keys, rpm_limit=95)

    # 解包 MOBI
    temp_dir, _ = mobi.extract(mobi_path)  # _ 是主文件路径
    # 进入解包后的目录（通常是 mobi7 子目录）
    mobi7_dir = os.path.join(temp_dir, 'mobi7')
    if not os.path.exists(mobi7_dir):
        # 尝试 mobi8 或直接使用 temp_dir
        mobi7_dir = temp_dir
    html_path = os.path.join(mobi7_dir, 'book.html')
    ncx_path = os.path.join(mobi7_dir, 'toc.ncx')

    if not os.path.exists(html_path) or not os.path.exists(ncx_path):
        raise FileNotFoundError(f"解包后未找到 book.html 或 toc.ncx，请检查 MOBI 文件。")

    # 解析章节列表
    chapters = parse_ncx(ncx_path)
    if not chapters:
        # 如果没有解析到任何章节，就当作整体一本书处理（只一个章节）
        chapters = [("全书", None)]

    # 准备输出目录
    book_name = Path(mobi_path).stem
    work_dir = os.path.join(output_base, book_name)
    # 检查是否已完成（复用原有完成标记逻辑）
    completed_marker = os.path.join(work_dir, COMPLETED_MARKER)
    if os.path.exists(completed_marker):
        logging.info(f"书籍 {book_name} 已完成，跳过。")
        print(f"【跳过】{book_name}（已完成）")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    audiobook_dir = os.path.join(work_dir, "mp3")
    os.makedirs(audiobook_dir, exist_ok=True)
    log_file = os.path.join(work_dir, "tts.log")

    # 获取根日志记录器，移除已有的所有 handlers
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # 创建文件处理器，不使用控制台处理器
    file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

    root_logger.addHandler(file_handler)
    root_logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
    # 读取整个 book.html
    with open(html_path, 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f, 'html.parser')

    # 获取所有锚点 id 的列表（用于确定每个章节的结束位置）
    all_anchor_ids = [anchor for (_, anchor) in chapters if anchor]
    # 构建章节上下文：每个章节包含 (标题, 起始id, 结束id)
    chapter_contexts = []
    for i, (title, anchor) in enumerate(chapters):
        next_anchor = all_anchor_ids[i+1] if i+1 < len(all_anchor_ids) else None
        chapter_contexts.append((title, anchor, next_anchor))

    # 逐章处理
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)  # 可选：仍然支持段落级并发，但这里我们按章节顺序执行
    for idx, (title, anchor, next_anchor) in enumerate(chapter_contexts):
        safe_title = f"{idx+1:04d}_{sanitize_filename(title) or f'Chapter_{idx+1}'}"
        output_mp3 = Path(audiobook_dir) / f"{safe_title}.{AUDIO_FORMAT}"
        if output_mp3.exists():
            logging.info(f"已存在，跳过: {output_mp3.name}")
            continue

        logging.info(f"合成章节: {safe_title}")
        # 提取该章节的 HTML 片段
        if anchor is None:
            # 无锚点，则使用整个 HTML（第一个章节可能用整个文档）
            chapter_soup = soup
        else:
            chapter_soup = extract_chapter_soup(soup, anchor, next_anchor)

        # 从 chapter_soup 中提取所有正文段落
        paragraphs = extract_original_paragraphs(chapter_soup)  # 可以直接用你之前定义的函数
        if not paragraphs:
            # 避免空章节，添加标题作为段落
            if title:
                paragraphs = [title]

        # 临时目录存放该章节的 wav 片段
        temp_dir_chap = Path(work_dir) / f"temp_chap_{idx:04d}"
        temp_dir_chap.mkdir(parents=True, exist_ok=True)

        # 并发合成段落（复用原逻辑）
        futures = {}
        for pi, para in enumerate(paragraphs):
            future = executor.submit(
                synthesize_paragraph_with_retry, para, pi, temp_dir_chap, tts_client, voice, style
            )
            futures[future] = pi

        for future in tqdm(as_completed(futures), total=len(paragraphs),
                           desc=f"章节 {idx+1}/{len(chapter_contexts)}"):
            pi = futures[future]
            try:
                _ = future.result()
            except Exception as e:
                logging.error(f"段落 {pi} 合成失败: {e}")

        # 合并为 MP3
        merge_paragraphs_to_chapter(temp_dir_chap, safe_title, Path(audiobook_dir), AUDIO_FORMAT)
        shutil.rmtree(temp_dir_chap, ignore_errors=True)

        # 可选：保存进度（但不是必需的，因为 MOBI 按顺序处理完一章就过）
        # 如果希望断点续传，可以保存每一章的完成状态，但先不增加复杂度

    executor.shutdown(wait=True)

    # 提取元数据（可以从 NCX 或 HTML 中获取，简单生成）
    # 这里复用原有 extract_epub_metadata 不太合适，可以简化：写入一个 podcast.json
    opf_path = os.path.join(mobi7_dir, 'content.opf')
    if not os.path.exists(opf_path):
        opf_path = os.path.join(temp_dir, 'content.opf')
    if os.path.exists(opf_path):
        meta = parse_opf_metadata(opf_path)
    else:
        meta = {
            "title": book_name,
            "author": "未知作者",
            "description": f"{book_name} - 有声书（由 MOBI 生成）",
            "language": "zh-CN"
        }
        
    podcast_meta = {
        "title": meta.get("title", book_name),
        "author": meta.get("author", "未知作者"),
        "description": meta.get("description", f"{meta.get('title', book_name)} - 有声书"),
        "language": meta.get("language", "zh-CN"),
        "explicit": False,
        "category": "Arts/Books",
        "publisher": meta.get("publisher", ""),
        "publish_date": meta.get("date", "")
    }
    with open(os.path.join(work_dir, "podcast.json"), "w", encoding="utf-8") as f:
        json.dump(podcast_meta, f, ensure_ascii=False, indent=2)

    # 完成标记
    Path(completed_marker).touch()
    logging.info(f"书籍 {book_name} 处理完毕！")
    print(f"【完成】{book_name}")

    # 清理解包的临时文件
    shutil.rmtree(temp_dir, ignore_errors=True)
def process_ebook(ebook_path: str, output_base: str = DEFAULT_OUTPUT_DIR,
                  voice: str = DEFAULT_VOICE, style: str = DEFAULT_STYLE,
                  tts_api_keys: List[str] = None):
    if tts_api_keys is None:
        tts_api_keys = os.environ.get("MIMO_TTS_API_KEYS", "").split(",")
        tts_api_keys = [k.strip() for k in tts_api_keys if k.strip()]
    ext = Path(ebook_path).suffix.lower()
    if ext == '.epub':
        process_epub(ebook_path, output_base, voice, style, tts_api_keys)
    elif ext == '.mobi':
        process_mobi(ebook_path, output_base, voice, style, tts_api_keys)
    else:
        raise ValueError(f"不支持的文件格式: {ext}")
# ============================
# 批量处理入口
# ============================
def process_batch(input_path: str, output_base: str, voice: str, style: str, api_keys: List[str]):
    input_path = Path(input_path)
    if input_path.is_dir():
        epub_files = sorted(input_path.glob("*.epub"))
        if not epub_files:
            print(f"目录 {input_path} 中没有找到 .epub 文件。")
            return
        print(f"找到 {len(epub_files)} 个 EPUB 文件，开始处理...")
        for epub_file in epub_files:
            print(f"\n>> 正在处理: {epub_file.name}")
            try:
                process_epub(str(epub_file), output_base=output_base,
                             voice=voice, style=style, tts_api_keys=api_keys)
            except Exception as e:
                logging.error(f"处理 {epub_file} 时出错: {e}")
                print(f"【错误】{epub_file.name} 处理失败: {e}")
    elif input_path.is_file() and input_path.suffix.lower() == ".epub":
        process_epub(str(input_path), output_base=output_base,
                     voice=voice, style=style, tts_api_keys=api_keys)
    else:
        print(f"错误: '{input_path}' 不是有效的 EPUB 文件或目录。")



# ============================
# 命令行接口
# ============================
if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="EPUB 转有声书工具 (MiMo TTS)")
    parser.add_argument("input", help="输入的 EPUB/MOBI 文件或包含电子书的目录")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_DIR, help="输出根目录")
    parser.add_argument("--voice", default=None, help="TTS 音色")
    parser.add_argument("--style", default=None, help="TTS 风格描述")
    parser.add_argument("--api-key", default=None, help="MiMo API 密钥（单个，可多次使用）")
    parser.add_argument("--api-keys", nargs='+', default=None, help="多个 API 密钥，空格分隔")
    parser.add_argument("--config", default=None, help="JSON 配置文件路径")

    args = parser.parse_args()

    # ---- 加载配置文件 ----
    config = {}
    config_path = args.config
    if config_path:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

    # ---- 参数优先级：命令行 > 配置文件 > 环境变量 > 默认值 ----
    # 1. 语音
    voice = args.voice or config.get("voice") or os.environ.get("MIMO_TTS_VOICE") or DEFAULT_VOICE

    # 2. 风格
    style = args.style or config.get("style") or os.environ.get("MIMO_TTS_STYLE") or DEFAULT_STYLE

    # 3. API 密钥列表
    api_keys = []
    if config.get("api_keys"):
        api_keys.extend(config["api_keys"])
    if args.api_key:
        api_keys.append(args.api_key)
    if args.api_keys:
        api_keys.extend(args.api_keys)
    # 如果命令行和配置文件都没给，尝试环境变量（逗号分隔）
    if not api_keys:
        env_keys = os.environ.get("MIMO_TTS_API_KEYS", "")
        if env_keys:
            api_keys = [k.strip() for k in env_keys.split(",") if k.strip()]

    # 最终去重（保留顺序）
    seen = set()
    unique_keys = []
    for k in api_keys:
        if k not in seen:
            seen.add(k)
            unique_keys.append(k)
    api_keys = unique_keys

    if not api_keys:
        print("错误：请通过 --api-key/--api-keys/配置文件/环境变量提供至少一个 API 密钥。")
        sys.exit(1)

    # 调用入口（所有函数现在接收 api_keys: List[str]）
    process_batch(
        args.input,
        args.output,
        voice,
        style,
        api_keys
    )