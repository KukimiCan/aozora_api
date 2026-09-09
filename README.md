# Aozora Caching API

青空文庫から著作権フラグが「なし」でHTML本文がある作品を選び、冒頭をランダムに返します。[kawazu](https://github.com/KukimiCan/kawazu) のバックエンドです。

## 起動

Python 3.11以降を使ってください。CSVはこのリポジトリの既存ファイルを使用します。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

| 環境変数 | 内容 |
| --- | --- |
| `AOZORA_CSV_PATH` | CSVのパス。省略時は `main.py` と同じディレクトリの `list_person_all_extended.csv` |
| `FRONTEND_URL` | 許可するフロントのorigin。複数ならカンマ区切り。localhost:3000は常に許可 |

`uvicorn main:app --host 0.0.0.0 --port $PORT` でも起動できます。キャッシュはプロセスごとです。ワーカーを増やすと各プロセスが個別に補充するため、まず1ワーカーで開始してください。

## エンドポイント

| エンドポイント | 動作 |
| --- | --- |
| `GET /` | プロセスの稼働状態。`ready` はカタログの有無。外部サイトの到達性は意味しません |
| `GET /ready` | 有効なカタログがなければ503 |
| `GET /search?num_chars=500` | 単件。`num_chars` は1〜1000、既定200 |
| `GET /search/batch?count=3&num_chars=500` | URLが重複しない作品の配列。`count` は1〜5、既定3 |

単件の既存形式を維持します。一括取得はこのオブジェクトの配列です。

```json
{
  "name": "作品名",
  "author": "作者名",
  "content": "冒頭の文章…",
  "url": "https://www.aozora.gr.jp/cards/..."
}
```

一括取得は、時間内に取得できた分だけ返す場合があります（1〜count件）。1件もなければ503と `Retry-After: 5`、入力範囲外は422です。末尾の `…` は指定文字数に含めません。

## 安定性

- カタログ読み込み時に権利フラグ・URLを検査し、同一作品の行を重複排除します。
- 上流は青空文庫のHTTP(S) URLのみ許可し、HTTPSへ正規化。転送先も毎回検証します。
- 最大2並列、1要求全体の待機は8秒まで。最大8候補の試行、短い失敗時の待機、レスポンスサイズ上限を設けています。
- キャッシュ済みの作品は補充中でもすぐ返します。キャッシュには本文全体でなく冒頭1001文字までを最大20作品保存します。
- 背景の補充タスクとHTTPクライアントは終了時に閉じます。

## 検証

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

模擬HTTPと小さなCSVで、API契約、重複、欠損データ、文字数、通信失敗、期限、部分結果、リダイレクト、終了処理を検証します。テストは本番CSVや外部ネットワークを必要としません。

## 適用順序

このAPIの更新後にkawazuの更新を適用してください。`/search` の形式は維持しているので、旧フロントにも対応します。新フロントは旧APIのbatch未対応（404/405）にも対応します。
