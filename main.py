# aozora.py

# --- ファイル上部で必要なライブラリを追加 ---
import asyncio
from typing import List
from urllib.parse import urljoin
# ... (他のimportはそのまま) ...
import pandas as pd
import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from bs4 import XMLParsedAsHTMLWarning
import warnings
import os

# ... (FastAPIの初期化やCORS設定はそのまま) ...
# --------------------------------------------------------------------------
# 1. FastAPIアプリの初期化など
# --------------------------------------------------------------------------
warnings.filterwarnings('ignore', category=XMLParsedAsHTMLWarning)
app = FastAPI(title="Aozora Caching API", version="1.2.0") # versionを更新
origins = [
    "http://localhost:3000",
    os.getenv("FRONTEND_URL") # Renderで設定する本番URL
]

origins = [origin for origin in origins if origin]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ... (CSV読み込みはそのまま) ...
# --------------------------------------------------------------------------
# 2. アプリ起動時にCSVファイルを読み込む
# --------------------------------------------------------------------------
try:
    csv_filepath = 'list_person_all_extended.csv'
    df_aozora = pd.read_csv(
        csv_filepath,
        encoding='cp932',
        usecols=['作品名', '作品著作権フラグ', '姓', '名', 'XHTML/HTMLファイルURL']
    ).dropna(subset=['XHTML/HTMLファイルURL'])
except FileNotFoundError:
    df_aozora = None

# --------------------------------------------------------------------------
# 3. キャッシュ用の設定とデータ構造
# --------------------------------------------------------------------------
class NovelCache(BaseModel):
    name: str
    author: str
    content: str
    url: str  # ★ 変更点 1: キャッシュモデルにURLフィールドを追加

# 取得した小説を貯めておくキャッシュ
novel_cache: List[NovelCache] = []
CACHE_SIZE = 20 

# --------------------------------------------------------------------------
# 4. レスポンスモデルの定義
# --------------------------------------------------------------------------
class SearchResult(BaseModel):
    name: str
    author: str
    content: str
    url: str # ★ 変更点 1: レスポンスモデルにURLフィールドを追加

class HealthResponse(BaseModel):
    status: str
    timestamp: datetime
    version: str

# --------------------------------------------------------------------------
# 5. 小説を取得して整形するコアロジックを関数化
# --------------------------------------------------------------------------
def fetch_and_process_novel():
    """ランダムな小説を1件取得して処理する関数（同期処理）"""
    if df_aozora is None:
        return None
    
    try:
        novel_info = df_aozora.sample(n=1).iloc[0]

        if novel_info['作品著作権フラグ'] == "あり":
            return None

        base_url = "https://www.aozora.gr.jp/"
        relative_url = novel_info['XHTML/HTMLファイルURL']
        absolute_url = urljoin(base_url, relative_url.replace('../', ''))

        response = requests.get(absolute_url)
        response.raise_for_status()
        response.encoding = 'shift_jis'
        soup = BeautifulSoup(response.text, 'lxml')

        main_text_div = soup.find('div', class_='main_text')
        if not main_text_div:
            return None 

        for tag in main_text_div.find_all(['rt', 'rp']):
            tag.decompose()
        for br in main_text_div.find_all('br'):
            br.replace_with('\n')

        cleaned_text = main_text_div.get_text()
        lines = cleaned_text.splitlines()
        stripped_lines = [line.strip() for line in lines]
        normalized_text = '\n'.join(stripped_lines)
        final_text = re.sub(r'\n{3,}', '\n\n', normalized_text).strip()
        
        return NovelCache(
            name=novel_info['作品名'],
            author=f"{novel_info['姓']} {novel_info['名']}",
            content=final_text,
            url=absolute_url # ★ 変更点 2: 取得したURLをキャッシュオブジェクトに含める
        )
    except Exception as e:
        print(f"Error fetching novel: {e}")
        return None

# ... (バックグラウンドタスクと起動イベントはそのまま) ...
# --------------------------------------------------------------------------
# 6. バックグラウンドでキャッシュを補充し続けるタスク
# --------------------------------------------------------------------------
async def replenish_cache():
    while True:
        if len(novel_cache) < CACHE_SIZE:
            print(f"キャッシュ補充中... (現在 {len(novel_cache)}/{CACHE_SIZE} 件)")
            novel = await asyncio.to_thread(fetch_and_process_novel)
            if novel:
                novel_cache.append(novel)
        await asyncio.sleep(1)

# --------------------------------------------------------------------------
# 7. FastAPIの起動イベントでバックグラウンドタスクを開始
# --------------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    print("サーバーが起動しました。キャッシュの準備を開始します...")
    asyncio.create_task(replenish_cache())

# --------------------------------------------------------------------------
# 8. APIエンドポイントの定義
# --------------------------------------------------------------------------
@app.get("/", response_model=HealthResponse, summary="ヘルスチェック")
def health_check():
    return HealthResponse(status="healthy", timestamp=datetime.now(), version="1.2.0")

@app.get("/search", response_model=SearchResult, summary="キャッシュから小説の冒頭を取得")
async def get_cached_novel_intro(num_chars: int = Query(200, gt=0, le=1000, description="取得する冒頭の文字数")):
    if not novel_cache:
        print("キャッシュが空です。ライブ取得にフォールバックします...")
        novel = await asyncio.to_thread(fetch_and_process_novel)
        if not novel:
            raise HTTPException(status_code=503, detail="作品を取得できませんでした。")
    else:
        novel = novel_cache.pop(0)

    content = novel.content
    if len(content) > num_chars:
        intro_text = content[:num_chars] + '…'
    else:
        intro_text = content

    return SearchResult(
        name=novel.name,
        author=novel.author,
        content=intro_text,
        url=novel.url # ★ 変更点 3: 最終的なレスポンスにURLを含める
    )
