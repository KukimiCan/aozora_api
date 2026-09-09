"""Random, anonymous introductions from Aozora Bunko (Python 3.11+)."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import random
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

VERSION = "1.3.0"
CACHE_SIZE = 20
MAX_BATCH_SIZE = 5
REQUEST_DEADLINE = 8.0
MAX_ATTEMPTS = 8
MAX_HTML_BYTES = 2_000_000
CSV_PATH = Path(os.getenv("AOZORA_CSV_PATH", Path(__file__).with_name("list_person_all_extended.csv")))
logger = logging.getLogger(__name__)


class SearchResult(BaseModel):
    name: str
    author: str
    content: str
    url: str


class HealthResponse(BaseModel):
    status: str
    timestamp: datetime
    version: str
    ready: bool
    cached: int
    catalog_size: int


@dataclass(frozen=True)
class Candidate:
    name: str
    author: str
    url: str


def canonical_url(value: str) -> str | None:
    try:
        parts = urlsplit(urljoin("https://www.aozora.gr.jp/", value.strip()))
        if (parts.scheme not in {"http", "https"}
                or parts.hostname not in {"aozora.gr.jp", "www.aozora.gr.jp"}
                or parts.username or parts.password or parts.port):
            return None
        return urlunsplit(("https", "www.aozora.gr.jp", parts.path, "", ""))
    except ValueError:
        return None


def load_catalog(path: Path) -> list[Candidate]:
    columns = ["作品名", "作品著作権フラグ", "姓", "名", "XHTML/HTMLファイルURL"]
    try:
        frame = pd.read_csv(path, encoding="cp932", usecols=columns, dtype=str).fillna("")
    except (OSError, ValueError, UnicodeError, pd.errors.ParserError) as error:
        logger.error("Cannot load Aozora catalog: %s", error)
        return []
    works: dict[str, Candidate] = {}
    for row in frame.to_dict("records"):
        if row["作品著作権フラグ"].strip() != "なし" or not row["作品名"].strip() or not row["XHTML/HTMLファイルURL"].strip():
            continue
        url = canonical_url(row["XHTML/HTMLファイルURL"])
        if url:
            # The extended CSV can contain several contributors for one work.
            author = " ".join(part.strip() for part in [row["姓"], row["名"]] if part.strip())
            works.setdefault(url, Candidate(row["作品名"].strip(), author, url))
    return list(works.values())


def extract_intro(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("div", class_="main_text")
    if main is None:
        return None
    for tag in main.find_all(["rt", "rp", "script", "style"]):
        tag.decompose()
    for tag in main.find_all("br"):
        tag.replace_with("\n")
    # Preserve Japanese paragraph indentation, but remove markup-only blank lines.
    text = "\n".join(line.rstrip() for line in main.get_text().splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip(" \t\r\n")
    return text[:1001] if text else None


class NovelService:
    def __init__(self, candidates: list[Candidate], client: httpx.AsyncClient):
        self.candidates = candidates
        self.client = client
        self.cache: deque[SearchResult] = deque(maxlen=CACHE_SIZE)
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(2)
        self.failure_until = 0.0

    async def fetch_candidate(self, candidate: Candidate) -> SearchResult | None:
        try:
            async with self.slots:
                url = candidate.url
                # Validate every redirect; never follow to a different website.
                for _ in range(3):
                    async with self.client.stream("GET", url, follow_redirects=False) as response:
                        if response.is_redirect:
                            destination = canonical_url(urljoin(url, response.headers.get("location", "")))
                            if not destination or destination == url:
                                return None
                            url = destination
                            continue
                        response.raise_for_status()
                        chunks = bytearray()
                        async for chunk in response.aiter_bytes():
                            chunks.extend(chunk)
                            if len(chunks) > MAX_HTML_BYTES:
                                return None
                        # Aozora XHTML is Shift_JIS; cp932 includes its Windows extensions.
                        text = extract_intro(chunks.decode("cp932", errors="replace"))
                        if text:
                            return SearchResult(name=candidate.name, author=candidate.author, content=text, url=candidate.url)
                        return None
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("Aozora fetch failed: %s", type(error).__name__)
        return None

    async def take(self, count: int) -> list[SearchResult]:
        if not self.candidates:
            return []
        result: list[SearchResult] = []
        seen: set[str] = set()
        # Popping cached records has no await and is atomic on this event loop.
        # A slow background fetch must not delay already cached works.
        while self.cache and len(result) < count:
            novel = self.cache.popleft()
            if novel.url not in seen:
                result.append(novel)
                seen.add(novel.url)
        if len(result) == count:
            return result
        try:
            # Includes lock/semaphore waits, redirects and all retry rounds.
            async with asyncio.timeout(REQUEST_DEADLINE):
                async with self.lock:
                    while self.cache and len(result) < count:
                        novel = self.cache.popleft()
                        if novel.url not in seen:
                            result.append(novel)
                            seen.add(novel.url)
                    if len(result) == count or asyncio.get_running_loop().time() < self.failure_until:
                        return result
                    candidates = random.sample(self.candidates, min(MAX_ATTEMPTS, len(self.candidates)))
                    candidates = [candidate for candidate in candidates if candidate.url not in seen]
                    while candidates and len(result) < count:
                        width = min(2, count - len(result), len(candidates))
                        group, candidates = candidates[:width], candidates[width:]
                        fetched = await asyncio.gather(*(self.fetch_candidate(candidate) for candidate in group))
                        for novel in fetched:
                            if novel and novel.url not in seen:
                                seen.add(novel.url)
                                result.append(novel)
                    if not result:
                        self.failure_until = asyncio.get_running_loop().time() + 5
        except TimeoutError:
            self.failure_until = asyncio.get_running_loop().time() + 5
        return result

    async def replenish(self):
        while True:
            # Use the same lock as live misses; at most two upstream requests run
            # and concurrent cold requests do not duplicate the whole retry loop.
            if self.candidates and len(self.cache) < min(CACHE_SIZE, len(self.candidates)):
                try:
                    async with asyncio.timeout(REQUEST_DEADLINE):
                        async with self.lock:
                            if asyncio.get_running_loop().time() >= self.failure_until:
                                cached = {novel.url for novel in self.cache}
                                choices = [candidate for candidate in self.candidates if candidate.url not in cached]
                                if choices:
                                    novel = await self.fetch_candidate(random.choice(choices))
                                    if novel:
                                        self.cache.append(novel)
                                    else:
                                        self.failure_until = asyncio.get_running_loop().time() + 5
                except TimeoutError:
                    self.failure_until = asyncio.get_running_loop().time() + 5
            await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    candidates = await asyncio.to_thread(load_catalog, CSV_PATH)
    async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=3), limits=httpx.Limits(max_connections=2)) as client:
        service = NovelService(candidates, client)
        app.state.novels = service
        task = asyncio.create_task(service.replenish())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Aozora Caching API", version=VERSION, lifespan=lifespan)
origins = ["http://localhost:3000", *os.getenv("FRONTEND_URL", "").split(",")]
app.add_middleware(CORSMiddleware, allow_origins=[origin.strip().rstrip("/") for origin in origins if origin.strip()],
                   allow_credentials=False, allow_methods=["GET"], allow_headers=["*"])


def service_for(request: Request) -> NovelService:
    return request.app.state.novels


@app.get("/", response_model=HealthResponse, summary="プロセスの稼働状態")
def health_check(request: Request):
    service = service_for(request)
    return HealthResponse(status="healthy", timestamp=datetime.now(timezone.utc), version=VERSION,
                          ready=bool(service.candidates), cached=len(service.cache), catalog_size=len(service.candidates))


@app.get("/ready", summary="作品カタログの読み込み状態")
def readiness(request: Request):
    if not service_for(request).candidates:
        raise HTTPException(503, "作品カタログを読み込めませんでした。")
    return {"ready": True}


def excerpt(novel: SearchResult, num_chars: int) -> SearchResult:
    text = novel.content[:num_chars] + ("…" if len(novel.content) > num_chars else "")
    return SearchResult(name=novel.name, author=novel.author, content=text, url=novel.url)


async def get_novels(request: Request, count: int, num_chars: int) -> list[SearchResult]:
    novels = await service_for(request).take(count)
    if not novels:
        raise HTTPException(503, "作品を取得できませんでした。少し待ってから再試行してください。", headers={"Retry-After": "5"})
    return [excerpt(novel, num_chars) for novel in novels]


@app.get("/search", response_model=SearchResult, summary="ランダムな作品の冒頭を取得")
async def get_cached_novel_intro(request: Request, num_chars: int = Query(200, gt=0, le=1000)):
    return (await get_novels(request, 1, num_chars))[0]


@app.get("/search/batch", response_model=list[SearchResult], summary="重複のない冒頭を最大5件取得")
async def get_cached_novel_batch(request: Request, count: int = Query(3, ge=1, le=MAX_BATCH_SIZE),
                                 num_chars: int = Query(200, gt=0, le=1000)):
    # A partial batch is useful immediately; never discard it just because a later fetch failed.
    return await get_novels(request, count, num_chars)
