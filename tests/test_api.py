import asyncio
from contextlib import asynccontextmanager
import time

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import main


HTML = '<html><div class="main_text">吾輩は<ruby>猫<rt>ねこ</rt><rp>（</rp></ruby>である。<br />\u3000名前はまだ無い。</div></html>'


def candidate(index=1):
    return main.Candidate(f"作品{index}", "作者", f"https://www.aozora.gr.jp/cards/1/files/{index}.html")


def novel(index=1):
    work = candidate(index)
    return main.SearchResult(name=work.name, author=work.author, content="あいうえお", url=work.url)


@pytest.fixture
def client():
    async def upstream(request):
        return httpx.Response(200, content=HTML.encode("cp932"))

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
            app.state.novels = main.NovelService([candidate(i) for i in range(8)], http)
            yield

    app = main.app
    original = app.router.lifespan_context
    app.router.lifespan_context = lifespan
    with TestClient(app) as test_client:
        yield test_client
    app.router.lifespan_context = original


def test_single_contract_and_truncation(client):
    response = client.get('/search?num_chars=4')
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {'name', 'author', 'content', 'url'}
    assert data['content'] == '吾輩は猫…'
    assert data['url'].startswith('https://www.aozora.gr.jp/')


def test_batch_contract_and_uniqueness(client):
    response = client.get('/search/batch?count=5&num_chars=1000')
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 5
    assert len({item['url'] for item in data}) == 5
    assert 'ねこ' not in data[0]['content']
    assert '\n\u3000名前' in data[0]['content']


@pytest.mark.parametrize('path', ['/search?num_chars=0', '/search?num_chars=1001', '/search/batch?count=0', '/search/batch?count=6', '/search/batch?count=abc', '/search/batch?num_chars=-1'])
def test_parameter_bounds(client, path):
    assert client.get(path).status_code == 422


def test_liveness_is_distinct_from_catalog_readiness(client):
    assert client.get('/').json()['ready'] is True
    client.app.state.novels.candidates = []
    assert client.get('/').status_code == 200
    assert client.get('/').json()['ready'] is False
    assert client.get('/ready').status_code == 503
    response = client.get('/search')
    assert response.status_code == 503
    assert response.headers['retry-after'] == '5'


def test_partial_batch_on_small_catalog(client):
    client.app.state.novels.candidates = [candidate()]
    response = client.get('/search/batch?count=5')
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_catalog_filters_copyright_missing_values_and_duplicate_urls(tmp_path):
    columns = ['作品名', '作品著作権フラグ', '姓', '名', 'XHTML/HTMLファイルURL']
    rows = [
        ['作品', 'なし', '作者', None, candidate().url],
        ['同じ作品', 'なし', '翻訳者', '名', candidate().url.replace('https:', 'http:')],
        ['非公開', 'あり', '作者', '', candidate(2).url],
        ['不明', '', '作者', '', candidate(3).url],
        ['URLなし', 'なし', '作者', '', None],
        ['外部', 'なし', '作者', '', 'https://example.com/'],
    ]
    path = tmp_path / 'catalog.csv'
    pd.DataFrame(rows, columns=columns).to_csv(path, encoding='cp932', index=False)
    works = main.load_catalog(path)
    assert len(works) == 1
    assert works[0].author == '作者'
    assert works[0].url.startswith('https://')
    assert main.load_catalog(tmp_path / 'missing.csv') == []
    path.write_text('wrong,columns\n1,2\n')
    assert main.load_catalog(path) == []


def test_text_extraction_handles_missing_and_empty_body():
    assert main.extract_intro('<div>not a novel</div>') is None
    assert main.extract_intro('<div class="main_text"><br /></div>') is None
    assert len(main.extract_intro('<div class="main_text">' + '字' * 5000 + '</div>')) == 1001


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'https://www.aozora.gr.jp.evil.test/', 'http://localhost:8000', 'https://a:b@www.aozora.gr.jp/', 'https://www.aozora.gr.jp:123/'])
def test_url_allowlist(url):
    assert main.canonical_url(url) is None


def test_redirect_to_untrusted_host_is_never_fetched():
    async def run():
        calls = []
        async def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={'location': 'http://localhost/secret'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            service = main.NovelService([candidate()], http)
            assert await service.fetch_candidate(candidate()) is None
        assert calls == [candidate().url]
    asyncio.run(run())


def test_deadline_preserves_partial_results_and_cancels_fetches(monkeypatch):
    monkeypatch.setattr(main, 'REQUEST_DEADLINE', .05)
    async def run():
        async def slow(request):
            await asyncio.sleep(10)
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as http:
            service = main.NovelService([candidate(2)], http)
            service.cache.append(novel())
            start = time.monotonic()
            result = await service.take(3)
            assert time.monotonic() - start < .5
            assert [item.url for item in result] == [candidate().url]
    asyncio.run(run())


def test_warm_cache_does_not_wait_for_background_lock():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
            service = main.NovelService([candidate()], http)
            service.cache.append(novel())
            await service.lock.acquire()
            try:
                result = await asyncio.wait_for(service.take(1), .1)
                assert len(result) == 1
            finally:
                service.lock.release()
    asyncio.run(run())


def test_retries_are_bounded_and_failed_requests_cool_down():
    async def run():
        calls = 0
        async def unavailable(request):
            nonlocal calls
            calls += 1
            return httpx.Response(503)
        async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as http:
            service = main.NovelService([candidate(i) for i in range(20)], http)
            assert await service.take(1) == []
            assert calls == main.MAX_ATTEMPTS
            assert await service.take(1) == []
            assert calls == main.MAX_ATTEMPTS
    asyncio.run(run())


def test_oversized_response_is_not_cached(monkeypatch):
    monkeypatch.setattr(main, 'MAX_HTML_BYTES', 10)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'x' * 100))) as http:
            service = main.NovelService([candidate()], http)
            assert await service.fetch_candidate(candidate()) is None
    asyncio.run(run())


def test_service_shutdown_cancels_replenishment(monkeypatch):
    monkeypatch.setattr(main, 'load_catalog', lambda path: [])
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kwargs: mock_client)
    async def run():
        async with main.lifespan(main.app):
            assert main.app.state.novels.candidates == []
            tasks = [task for task in asyncio.all_tasks() if task.get_coro().__name__ == 'replenish']
            assert len(tasks) == 1
        assert tasks[0].cancelled()
    asyncio.run(run())
