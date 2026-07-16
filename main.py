import os
import re
import json
import time
import logging
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from google import genai
from pydantic import BaseModel, Field
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# =============================================================================
# 기본 설정
# =============================================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# 운영 중 모델명이 바뀌면 환경변수만 변경할 수 있도록 분리
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").strip()

MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", "5"))
ARTICLE_TEXT_LIMIT = int(os.environ.get("ARTICLE_TEXT_LIMIT", "12000"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "20"))

KST = ZoneInfo("Asia/Seoul")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


KEYWORDS = (
    '("배전망 ESS" OR "배전망 에너지저장장치" OR "AI 활용 ESS" '
    'OR "VPP" OR "가상발전소" OR "재생에너지 계통연계" '
    'OR "접속대기" OR "계통혼잡" OR "출력제어" '
    'OR "동적운영한계" OR "Dynamic Operating Envelope" OR "BESS")'
)

ENCODED_QUERY = urllib.parse.quote(KEYWORDS)
RSS_URL = (
    "https://news.google.com/rss/search"
    f"?q={ENCODED_QUERY}&hl=ko&gl=KR&ceid=KR:ko"
)


# =============================================================================
# 자료형
# =============================================================================

@dataclass
class ArticleData:
    index: int
    title: str
    source: str
    url: str
    author: str
    email: str
    published_at: str
    date_basis: str
    body: str
    rss_summary: str


class ArticleSummary(BaseModel):
    one_line_summary: str = Field(
        description="기사의 핵심 주제를 객관적으로 요약한 한 문장"
    )
    details: list[str] = Field(
        description="기사 본문에서 확인되는 핵심 세부 내용 두 개"
    )


# =============================================================================
# HTTP / 환경 설정
# =============================================================================

def validate_environment() -> None:
    missing = [
        name
        for name, value in {
            "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
            "CHAT_ID": CHAT_ID,
            "GEMINI_API_KEY": GEMINI_API_KEY,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(
            "필수 환경변수가 없습니다: " + ", ".join(missing)
        )


def build_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.5",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
        }
    )
    return session


# =============================================================================
# Google News RSS 주소 → 실제 언론사 주소 변환
# =============================================================================

def is_google_news_url(url: str) -> bool:
    try:
        return urlparse(url).hostname in {"news.google.com", "www.news.google.com"}
    except Exception:
        return False


def decode_google_news_url(
    google_url: str,
    session: requests.Session,
) -> str:
    """
    Google News RSS의 암호화된 기사 주소를 실제 언론사 주소로 변환합니다.

    단순 requests.get(...).url 방식으로는 Google News 페이지에 머무르는 경우가
    많으므로, Google News 페이지의 signature/timestamp를 읽은 뒤
    batchexecute 응답에서 원문 URL을 추출합니다.

    Google의 내부 형식이 변경되면 실패할 수 있으므로 항상 원래 주소를
    fallback으로 반환합니다.
    """
    if not is_google_news_url(google_url):
        return google_url

    try:
        parsed = urlparse(google_url)
        parts = [part for part in parsed.path.split("/") if part]

        if len(parts) < 2 or parts[-2] not in {"articles", "read"}:
            logger.warning("지원하지 않는 Google News URL 형식: %s", google_url)
            return google_url

        article_id = parts[-1]
        signature = ""
        timestamp = ""

        candidate_urls = [
            f"https://news.google.com/articles/{article_id}",
            f"https://news.google.com/rss/articles/{article_id}",
        ]

        for candidate in candidate_urls:
            response = session.get(candidate, timeout=REQUEST_TIMEOUT)
            if response.status_code >= 400:
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            data_element = (
                soup.select_one("[data-n-a-sg][data-n-a-ts]")
                or soup.select_one("c-wiz > div[jscontroller]")
            )

            if data_element:
                signature = data_element.get("data-n-a-sg", "")
                timestamp = data_element.get("data-n-a-ts", "")

            if signature and timestamp:
                break

        if not signature or not timestamp:
            logger.warning("Google News 디코딩 파라미터 추출 실패")
            return google_url

        payload = [
            "Fbv4je",
            (
                '["garturlreq",'
                '[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
                'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,'
                f'null,0,0,null,0],"{article_id}",{timestamp},"{signature}"]'
            ),
        ]

        endpoint = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
        request_body = f"f.req={urllib.parse.quote(json.dumps([[payload]]))}"

        response = session.post(
            endpoint,
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "User-Agent": session.headers["User-Agent"],
            },
            data=request_body,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        response_parts = response.text.split("\n\n")
        if len(response_parts) < 2:
            raise ValueError("Google News batchexecute 응답 형식이 예상과 다릅니다.")

        parsed_data = json.loads(response_parts[1])[:-2]
        decoded_url = json.loads(parsed_data[0][2])[1]

        if (
            isinstance(decoded_url, str)
            and decoded_url.startswith(("http://", "https://"))
            and not is_google_news_url(decoded_url)
        ):
            return decoded_url

        raise ValueError("디코딩 결과가 유효한 언론사 URL이 아닙니다.")

    except Exception as exc:
        logger.warning("Google News 원문 URL 변환 실패: %s", exc)
        return google_url


# =============================================================================
# 기사 메타데이터·본문 추출
# =============================================================================

def first_nonempty(values: Iterable[Any]) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_author(author: str) -> str:
    author = normalize_whitespace(author)
    author = re.sub(r"^(by|작성자|기자명)\s*[:：]?\s*", "", author, flags=re.I)
    author = re.sub(r"\s*[|·]\s*.*$", "", author)
    author = re.sub(r"\s*(기자|특파원|에디터)\s*$", "", author)
    author = re.sub(r"\([^)]*@[^)]*\)", "", author)
    author = normalize_whitespace(author)

    # URL·이메일·지나치게 긴 문구는 기자명으로 사용하지 않음
    if (
        not author
        or "http://" in author
        or "https://" in author
        or "@" in author
        or len(author) > 50
    ):
        return ""

    return author


def normalize_date(raw_date: str) -> str:
    raw_date = normalize_whitespace(raw_date)
    if not raw_date:
        return ""

    # 기사 페이지에서 흔히 보이는 한국식 날짜 표현을 우선 처리
    korean_match = re.search(
        r"(?P<year>20\d{2})[.\-/년]\s*"
        r"(?P<month>\d{1,2})[.\-/월]\s*"
        r"(?P<day>\d{1,2})(?:일)?"
        r"(?:\s+|T)?"
        r"(?P<hour>\d{1,2})?"
        r"(?::(?P<minute>\d{2}))?",
        raw_date,
    )

    if korean_match:
        parts = korean_match.groupdict()
        dt = datetime(
            int(parts["year"]),
            int(parts["month"]),
            int(parts["day"]),
            int(parts["hour"] or 0),
            int(parts["minute"] or 0),
            tzinfo=KST,
        )
        return dt.strftime("%Y.%m.%d %H:%M")

    try:
        dt = date_parser.parse(raw_date, fuzzy=True)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        else:
            dt = dt.astimezone(KST)
        return dt.strftime("%Y.%m.%d %H:%M")
    except Exception:
        return ""


def rss_date_to_kst(entry: Any) -> str:
    for key in ("published", "updated"):
        raw = entry.get(key, "")
        if not raw:
            continue

        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            else:
                dt = dt.astimezone(KST)
            return dt.strftime("%Y.%m.%d %H:%M")
        except Exception:
            normalized = normalize_date(raw)
            if normalized:
                return normalized

    return ""


def iter_json_objects(value: Any) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_objects(child)


def parse_json_ld(soup: BeautifulSoup) -> list[dict]:
    objects: list[dict] = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue

        try:
            data = json.loads(raw)
            objects.extend(iter_json_objects(data))
        except (json.JSONDecodeError, TypeError):
            continue

    return objects


def extract_jsonld_author(objects: list[dict]) -> str:
    for obj in objects:
        author = obj.get("author")
        if not author:
            continue

        candidates = author if isinstance(author, list) else [author]

        for candidate in candidates:
            if isinstance(candidate, dict):
                name = clean_author(candidate.get("name", ""))
            else:
                name = clean_author(str(candidate))

            if name:
                return name

    return ""


def extract_jsonld_date(objects: list[dict]) -> str:
    for obj in objects:
        for key in ("datePublished", "dateCreated", "uploadDate"):
            normalized = normalize_date(obj.get(key, ""))
            if normalized:
                return normalized
    return ""


def extract_jsonld_body(objects: list[dict]) -> str:
    for obj in objects:
        body = normalize_whitespace(obj.get("articleBody", ""))
        if len(body) >= 200:
            return body
    return ""


def find_meta_content(
    soup: BeautifulSoup,
    *,
    names: tuple[str, ...] = (),
    properties: tuple[str, ...] = (),
    itemprops: tuple[str, ...] = (),
) -> str:
    lowered_names = {value.lower() for value in names}
    lowered_properties = {value.lower() for value in properties}
    lowered_itemprops = {value.lower() for value in itemprops}

    for meta in soup.find_all("meta"):
        name = str(meta.get("name", "")).lower()
        prop = str(meta.get("property", "")).lower()
        itemprop = str(meta.get("itemprop", "")).lower()
        content = normalize_whitespace(meta.get("content", ""))

        if not content:
            continue

        if (
            name in lowered_names
            or prop in lowered_properties
            or itemprop in lowered_itemprops
        ):
            return content

    return ""


def extract_email(soup: BeautifulSoup, page_text: str) -> str:
    for anchor in soup.select('a[href^="mailto:"]'):
        email = anchor.get("href", "").replace("mailto:", "").split("?")[0].strip()
        if re.fullmatch(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", email):
            return email

    email_pattern = re.compile(
        r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])"
    )

    for email in email_pattern.findall(page_text[:15000]):
        lower = email.lower()
        if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            continue
        if "example." in lower or "wixpress.com" in lower:
            continue
        return email

    return ""


def extract_author_from_visible_text(soup: BeautifulSoup, page_text: str) -> str:
    selectors = [
        '[class*="byline"]',
        '[class*="author"]',
        '[class*="reporter"]',
        '[class*="writer"]',
        '[class*="journalist"]',
        '[itemprop="author"]',
    ]

    for selector in selectors:
        for node in soup.select(selector)[:5]:
            candidate = clean_author(node.get_text(" ", strip=True))
            if candidate:
                return candidate

    # 기사 상단에 표시되는 "홍길동 기자" 형태
    match = re.search(
        r"(?<![가-힣A-Za-z])"
        r"([가-힣]{2,5}|[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,2})"
        r"\s*(기자|특파원|에디터)",
        page_text[:6000],
    )
    if match:
        return clean_author(match.group(1))

    return ""


def extract_date_from_visible_text(page_text: str) -> str:
    head = page_text[:8000]

    patterns = [
        r"(?:승인|입력|등록|게재|발행|수정)\s*[:：]?\s*"
        r"(20\d{2}[.\-/년]\s*\d{1,2}[.\-/월]\s*\d{1,2}(?:일)?"
        r"(?:\s+|T)?\d{0,2}:?\d{0,2})",
        r"(20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}"
        r"(?:\s+|T)\d{1,2}:\d{2})",
    ]

    for pattern in patterns:
        match = re.search(pattern, head)
        if match:
            normalized = normalize_date(match.group(1))
            if normalized:
                return normalized

    return ""


def extract_article_body(soup: BeautifulSoup, json_objects: list[dict]) -> str:
    json_body = extract_jsonld_body(json_objects)
    if json_body:
        return json_body[:ARTICLE_TEXT_LIMIT]

    # 광고·메뉴·스크립트 제거
    for tag in soup(
        [
            "script",
            "style",
            "noscript",
            "svg",
            "header",
            "footer",
            "nav",
            "aside",
            "form",
            "button",
        ]
    ):
        tag.decompose()

    article = soup.find("article")
    container = article or soup.find("main") or soup.body or soup

    paragraphs: list[str] = []
    seen: set[str] = set()

    for paragraph in container.find_all("p"):
        text = normalize_whitespace(paragraph.get_text(" ", strip=True))
        if len(text) < 25 or text in seen:
            continue
        seen.add(text)
        paragraphs.append(text)

    body = "\n".join(paragraphs)

    if len(body) < 300:
        body = normalize_whitespace(container.get_text(" ", strip=True))

    return body[:ARTICLE_TEXT_LIMIT]


def extract_canonical_url(soup: BeautifulSoup, current_url: str) -> str:
    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical:
        href = canonical.get("href", "").strip()
        if href.startswith(("http://", "https://")) and not is_google_news_url(href):
            return href

    og_url = find_meta_content(soup, properties=("og:url",))
    if og_url.startswith(("http://", "https://")) and not is_google_news_url(og_url):
        return og_url

    return current_url


def fetch_article_data(
    index: int,
    entry: Any,
    session: requests.Session,
) -> ArticleData:
    rss_title = normalize_whitespace(entry.get("title", "제목 없음"))
    source = normalize_whitespace(
        getattr(getattr(entry, "source", None), "title", "")
    ) or "신문사 미상"

    # Google News 제목 끝에 붙는 " - 언론사" 제거
    suffix = f" - {source}"
    title = rss_title[:-len(suffix)].strip() if rss_title.endswith(suffix) else rss_title

    google_url = entry.get("link", "")
    decoded_url = decode_google_news_url(google_url, session)

    rss_summary = BeautifulSoup(
        entry.get("summary", ""),
        "html.parser",
    ).get_text(" ", strip=True)
    rss_summary = normalize_whitespace(rss_summary)

    fallback_date = rss_date_to_kst(entry)

    if is_google_news_url(decoded_url):
        logger.warning("[%s] 원문 URL 확보 실패: %s", index, title)
        return ArticleData(
            index=index,
            title=title,
            source=source,
            url=google_url,
            author="",
            email="",
            published_at=fallback_date,
            date_basis="RSS",
            body=rss_summary,
            rss_summary=rss_summary,
        )

    try:
        response = session.get(decoded_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()
        if "html" not in content_type and "xml" not in content_type:
            raise ValueError(f"HTML 문서가 아닙니다: {content_type}")

        if response.encoding and response.encoding.lower() in {"iso-8859-1", "ascii"}:
            response.encoding = response.apparent_encoding or "utf-8"

        soup = BeautifulSoup(response.text, "html.parser")
        json_objects = parse_json_ld(soup)
        page_text = normalize_whitespace(soup.get_text(" ", strip=True))

        meta_author = find_meta_content(
            soup,
            names=("author", "byl", "parsely-author", "dc.creator"),
            properties=("article:author",),
            itemprops=("author",),
        )

        author = first_nonempty(
            [
                clean_author(meta_author),
                extract_jsonld_author(json_objects),
                extract_author_from_visible_text(soup, page_text),
            ]
        )

        email = extract_email(soup, page_text)

        meta_date = find_meta_content(
            soup,
            names=(
                "pubdate",
                "publishdate",
                "date",
                "datepublished",
                "article_date_original",
                "parsely-pub-date",
                "dc.date",
                "dcterms.date",
            ),
            properties=(
                "article:published_time",
                "og:published_time",
                "datepublished",
            ),
            itemprops=("datepublished", "datecreated"),
        )

        time_tag = soup.find("time", attrs={"datetime": True})
        time_date = normalize_date(time_tag.get("datetime", "")) if time_tag else ""

        published_at = first_nonempty(
            [
                normalize_date(meta_date),
                extract_jsonld_date(json_objects),
                time_date,
                extract_date_from_visible_text(page_text),
                fallback_date,
            ]
        )

        date_basis = "기사 원문" if published_at and published_at != fallback_date else "RSS"

        extracted_title = first_nonempty(
            [
                find_meta_content(soup, properties=("og:title",)),
                find_meta_content(soup, names=("twitter:title",)),
                title,
            ]
        )

        body = extract_article_body(soup, json_objects)
        canonical_url = extract_canonical_url(soup, response.url)

        logger.info(
            "[%s] 원문 추출 완료 | 기자=%s | 날짜=%s | URL=%s",
            index,
            author or "없음",
            published_at or "없음",
            canonical_url,
        )

        return ArticleData(
            index=index,
            title=extracted_title,
            source=source,
            url=canonical_url,
            author=author,
            email=email,
            published_at=published_at,
            date_basis=date_basis,
            body=body or rss_summary,
            rss_summary=rss_summary,
        )

    except Exception as exc:
        logger.warning("[%s] 기사 페이지 추출 실패: %s", index, exc)
        return ArticleData(
            index=index,
            title=title,
            source=source,
            url=decoded_url,
            author="",
            email="",
            published_at=fallback_date,
            date_basis="RSS",
            body=rss_summary,
            rss_summary=rss_summary,
        )


# =============================================================================
# Gemini 요약
# =============================================================================

def summarize_article(
    client: genai.Client,
    article: ArticleData,
) -> ArticleSummary:
    source_text = article.body or article.rss_summary or article.title

    prompt = f"""
당신은 배전망·전력계통·에너지저장장치(ESS) 분야의 기술 분석가입니다.
아래 기사에 명시된 사실만 사용하여 부서장 보고용 요약을 작성하십시오.

[작성 원칙]
- 제목이나 본문에 없는 사실, 수치, 사업 목적, 기대효과를 추정하지 마십시오.
- '업계에서는', '기대된다' 같은 근거 없는 확대 해석을 하지 마십시오.
- 핵심 한 줄은 100자 안팎으로 작성하십시오.
- 세부 내용은 정확히 2개로 작성하고 각각 160자 이내로 작성하십시오.
- 전력계통·ESS 전문용어는 기사 문맥에 맞게 정확히 사용하십시오.
- 기자명, 이메일, 승인일, URL은 요약하지 마십시오. 해당 정보는 Python이 별도로 처리합니다.
- 기사 정보가 부족하면 부족한 범위 안에서만 요약하십시오.

[기사 제목]
{article.title}

[신문사]
{article.source}

[기사 본문]
{source_text[:ARTICLE_TEXT_LIMIT]}
""".strip()

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": ArticleSummary,
                "temperature": 0.1,
            },
        )

        parsed = response.parsed
        if isinstance(parsed, ArticleSummary):
            result = parsed
        else:
            result = ArticleSummary.model_validate(parsed)

        details = [
            normalize_whitespace(item)
            for item in result.details
            if normalize_whitespace(item)
        ][:2]

        while len(details) < 2:
            details.append("기사 원문에서 추가 세부사항을 확인하기 어렵습니다.")

        return ArticleSummary(
            one_line_summary=normalize_whitespace(result.one_line_summary),
            details=details,
        )

    except Exception as exc:
        logger.exception("[%s] Gemini 요약 실패: %s", article.index, exc)

        fallback = article.rss_summary or article.title
        return ArticleSummary(
            one_line_summary=normalize_whitespace(fallback)[:180],
            details=[
                "AI 요약 처리에 실패하여 RSS에서 확인된 내용만 표시합니다.",
                "세부 내용은 기사 원문 링크에서 확인이 필요합니다.",
            ],
        )


# =============================================================================
# 출력 형식 및 Telegram 전송
# =============================================================================

def format_author(author: str) -> str:
    if not author:
        return "기자명 확인 불가"
    return f"{author} 기자"


def format_date(article: ArticleData) -> str:
    if not article.published_at:
        return "승인일 확인 불가"

    if article.date_basis == "RSS":
        return f"승인 {article.published_at} (RSS 게시시각 기준)"

    return f"승인 {article.published_at}"


def format_briefing_item(
    article: ArticleData,
    summary: ArticleSummary,
) -> str:
    email = article.email or "이메일 미공개"
    details = summary.details[:2]

    return (
        f"{article.index}. [{article.title}]\n"
        f"ㅇ {format_author(article.author)} | {email} | "
        f"{format_date(article)} | {article.source}\n"
        f"ㅇ {summary.one_line_summary}\n"
        f"  - {details[0]}\n"
        f"  - {details[1]}\n"
        f"  - 기사 원문 링크: {article.url}"
    )


def split_telegram_message(text: str, max_length: int = 3900) -> list[str]:
    """
    Telegram sendMessage 제한(4096자)을 넘지 않도록 문단 단위로 분할합니다.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current = ""

    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph

        if len(candidate) <= max_length:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        # 단일 문단 자체가 너무 긴 경우 줄 단위로 재분할
        for line in paragraph.splitlines():
            line_candidate = f"{current}\n{line}".strip() if current else line

            if len(line_candidate) <= max_length:
                current = line_candidate
            else:
                if current:
                    chunks.append(current)
                for start in range(0, len(line), max_length):
                    piece = line[start:start + max_length]
                    if len(piece) == max_length:
                        chunks.append(piece)
                    else:
                        current = piece

    if current:
        chunks.append(current)

    return chunks


def send_telegram_message(
    session: requests.Session,
    text: str,
) -> None:
    endpoint = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunks = split_telegram_message(text)

    for number, chunk in enumerate(chunks, start=1):
        payload = {
            "chat_id": CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": True,
        }

        response = session.post(
            endpoint,
            data=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API 오류: {result}")

        logger.info("Telegram 메시지 전송 완료 (%s/%s)", number, len(chunks))

        # 동일 채팅에 너무 빠르게 연속 전송하지 않도록 짧게 간격 부여
        if number < len(chunks):
            time.sleep(1.1)


# =============================================================================
# 메인 처리
# =============================================================================

def get_news_and_summarize() -> None:
    validate_environment()

    session = build_session()
    client = genai.Client(api_key=GEMINI_API_KEY)

    try:
        feed = feedparser.parse(RSS_URL)

        if getattr(feed, "bozo", False):
            logger.warning("RSS 파싱 경고: %s", getattr(feed, "bozo_exception", ""))

        entries = list(feed.entries[:MAX_ARTICLES])

        if not entries:
            send_telegram_message(
                session,
                "👀 현재 시간 기준으로 검색된 배전망·ESS 뉴스가 없습니다.",
            )
            return

        briefing_items: list[str] = []

        for index, entry in enumerate(entries, start=1):
            article = fetch_article_data(index, entry, session)
            summary = summarize_article(client, article)
            briefing_items.append(format_briefing_item(article, summary))

            # Google News 디코딩 요청 간 과도한 호출 방지
            if index < len(entries):
                time.sleep(1.0)

        final_message = (
            "⚡ [배전망/ESS 주요 뉴스 브리핑]\n\n"
            + "\n\n".join(briefing_items)
        )

        send_telegram_message(session, final_message)

    except Exception as exc:
        logger.exception("뉴스 브리핑 처리 중 오류 발생: %s", exc)

        try:
            send_telegram_message(
                session,
                f"🚨 뉴스 브리핑 처리 중 오류가 발생했습니다.\n{type(exc).__name__}: {exc}",
            )
        except Exception:
            logger.exception("오류 알림 Telegram 전송도 실패했습니다.")

        raise

    finally:
        session.close()
        close_client = getattr(client, "close", None)
        if callable(close_client):
            close_client()


if __name__ == "__main__":
    get_news_and_summarize()
