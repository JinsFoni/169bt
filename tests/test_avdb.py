"""AVdb 客户端与 LTX 数据源用例（全部离线，用假 HTTP 替身）。

对应实现：``src/bt169/avdb.py``。
"""

from __future__ import annotations

import json

import pytest

from bt169 import config
from bt169 import avdb
from bt169.avdb import (
    AvdbAuthError,
    AvdbClient,
    AvdbError,
    AvdbNotConfigured,
    LtxSource,
    cover_proxy_url,
    image_fetch,
    load_creds,
    normalize_item,
)
from bt169.repo.settings import SettingsRepo


class Resp:
    def __init__(self, status, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHTTP:
    """按 URL 分派的假 httpx.Client。"""

    def __init__(self, *, login=None, search=None):
        self.login = login or Resp(200, {"code": 0, "data": {"access_token": "JWT1"}})
        self.search = search
        self.calls = []

    def post(self, url, *, json=None, data=None, headers=None, timeout=None):
        self.calls.append((url, json or data, headers or {}))
        if url.endswith("/api/v1/users/login"):
            if isinstance(self.login, list):
                return self.login.pop(0)
            return self.login
        if url.endswith("/api/v1/articles/search"):
            if callable(self.search):
                return self.search(json, headers or {})
            return self.search
        raise AssertionError(f"未预期的 URL：{url}")


def _items_payload(items, *, has_more):
    return Resp(200, {
        "code": 0,
        "data": {"items": items, "total": len(items), "hasMore": has_more},
    })


def _item(tid, *, date="2026-10-25", number="SSIS-157"):
    return {
        "tid": tid,
        "number": number,
        "title": "标题",
        "section": "LTX破解",
        "website": "ltx",
        "publish_date": date,
        "size_bytes": 3607826070,
        "magnet": "ed2k://|file|x.mp4|1|AB|/",
        "preview_images": "https://img/cover.jpg",
        "detail_url": "https://javdb.com/v/xxx",
        "create_time": f"{date}T12:00:00+08:00",
        "rejected": False,
    }


# ---------------------------------------------------------------- 设置读取


def test_load_creds_defaults_base_url(db, box):
    creds = load_creds(SettingsRepo(db, box))
    assert creds.base_url == config.AVDB_DEFAULT_BASE
    assert creds.configured is False       # 没账号密码 → 不可用


def test_load_creds_strips_trailing_slash(db, box):
    sr = SettingsRepo(db, box)
    sr.put(config.section_key("ltx", "base_url"), "http://1.2.3.4:8000/")
    sr.put(config.section_key("ltx", "username"), "u")
    sr.put(config.section_key("ltx", "password"), "p")
    creds = load_creds(sr)
    assert creds.base_url == "http://1.2.3.4:8000"
    assert creds.configured is True


# ---------------------------------------------------------------- 字段归一


def test_normalize_item_maps_fields():
    row = normalize_item(_item(42))
    assert row == {
        "id": 42,
        "number": "SSIS-157",
        "title": "标题",
        "section": "LTX破解",
        "publish_date": "2026-10-25",
        "size_bytes": 3607826070,
        "ed2k": "ed2k://|file|x.mp4|1|AB|/",
        "cover_url": "https://img/cover.jpg",
        "detail_url": "https://javdb.com/v/xxx",
        "rejected": 0,
        "created_at": "2026-10-25T12:00:00+08:00",
    }


def test_normalize_item_splits_comma_separated_preview_images():
    """★ 实测踩过的坑：``preview_images`` 有时是一串逗号分隔的 URL。

    整个字符串当 URL 去请求 → 图床 405 / 不是图片 → 界面一张破图。
    """
    raw = _item(1)
    raw["preview_images"] = (
        "https://awsimgsrc.dmm.com/a/ipx00770pl.jpg,"
        "https://awsimgsrc.dmm.com/a/ipx00770ps.jpg"
    )
    assert normalize_item(raw)["cover_url"] == (
        "https://awsimgsrc.dmm.com/a/ipx00770pl.jpg"
    )


def test_normalize_item_accepts_preview_images_list():
    raw = _item(1)
    raw["preview_images"] = ["", "https://img/a.jpg"]
    assert normalize_item(raw)["cover_url"] == "https://img/a.jpg"


def test_normalize_item_drops_non_url_cover():
    raw = _item(1)
    raw["preview_images"] = "not-a-url"
    assert normalize_item(raw)["cover_url"] is None


def test_normalize_item_falls_back_to_create_time_for_date():
    raw = _item(1)
    raw["publish_date"] = None
    assert normalize_item(raw)["publish_date"] == "2026-10-25"


def test_normalize_item_drops_rows_without_date():
    raw = _item(1)
    raw["publish_date"] = None
    raw["create_time"] = None
    assert normalize_item(raw) is None


def test_normalize_item_drops_rows_without_id():
    raw = _item(1)
    raw.pop("tid")
    assert normalize_item(raw) is None


def test_normalize_item_marks_rejected():
    raw = _item(1)
    raw["rejected"] = True
    assert normalize_item(raw)["rejected"] == 1


# ---------------------------------------------------------------- 登录


def test_client_login_returns_token():
    http = FakeHTTP()
    client = AvdbClient("http://x", http=http)
    assert client.login("u", "p") == "JWT1"
    url, data, headers = http.calls[0]
    assert url == "http://x/api/v1/users/login"
    assert data == {"username": "u", "password": "p"}     # 表单编码，不是 JSON


def test_client_login_without_credentials_raises_not_configured():
    with pytest.raises(AvdbNotConfigured):
        AvdbClient("http://x", http=FakeHTTP()).login("", "")


def test_client_login_2fa_raises_auth_error():
    http = FakeHTTP(login=Resp(200, {
        "code": 0, "data": {"access_token": "", "requires_2fa": True},
    }))
    with pytest.raises(AvdbAuthError):
        AvdbClient("http://x", http=http).login("u", "p")


def test_client_login_401_raises_auth_error():
    http = FakeHTTP(login=Resp(401, {"detail": "x"}))
    with pytest.raises(AvdbAuthError):
        AvdbClient("http://x", http=http).login("u", "p")


def test_client_login_http_500_raises_avdb_error():
    http = FakeHTTP(login=Resp(500, None, text="boom"))
    with pytest.raises(AvdbError):
        AvdbClient("http://x", http=http).login("u", "p")


def test_client_raises_on_business_error_code():
    http = FakeHTTP(login=Resp(200, {"code": 1001, "message": "令牌已失效"}))
    with pytest.raises(AvdbError, match="令牌已失效"):
        AvdbClient("http://x", http=http).login("u", "p")


def test_client_search_sends_bearer_and_body():
    http = FakeHTTP(search=_items_payload([_item(1)], has_more=False))
    client = AvdbClient("http://x", http=http)
    data = client.search("JWT1", page=2, page_size=50)
    assert data["hasMore"] is False
    url, body, headers = http.calls[-1]
    assert url == "http://x/api/v1/articles/search"
    assert headers["Authorization"] == "Bearer JWT1"
    assert body == {
        "page": 2, "page_size": 50,
        "website": "ltx", "section": "LTX破解",
    }


def test_client_search_rejects_missing_data():
    http = FakeHTTP(search=Resp(200, {"code": 0}))
    with pytest.raises(AvdbError, match="缺少 data"):
        AvdbClient("http://x", http=http).search("JWT1")


# ---------------------------------------------------------------- 全量拉取


@pytest.fixture()
def configured(db, box):
    sr = SettingsRepo(db, box)
    sr.put(config.section_key("ltx", "base_url"), "http://x")
    sr.put(config.section_key("ltx", "username"), "u")
    sr.put(config.section_key("ltx", "password"), "p")
    return sr


def test_fetch_all_paginates_until_has_more_false(configured):
    pages = {
        1: [_item(1), _item(2)],
        2: [_item(3)],
    }
    http = FakeHTTP(
        search=lambda body, headers: _items_payload(
            pages[body["page"]], has_more=body["page"] < 2
        )
    )
    rows = LtxSource(configured, http=http).fetch_all()
    assert [r["id"] for r in rows] == [1, 2, 3]
    # 登录一次 + 两页检索
    assert [c[0].rsplit("/api", 1)[-1] for c in http.calls] == [
        "/v1/users/login", "/v1/articles/search", "/v1/articles/search",
    ]


def test_fetch_all_caches_jwt_between_calls(configured):
    http = FakeHTTP(search=_items_payload([_item(1)], has_more=False))
    src = LtxSource(configured, http=http)
    src.fetch_all()
    src.fetch_all()
    logins = [c for c in http.calls if c[0].endswith("/users/login")]
    assert len(logins) == 1
    # 第二次复用的就是库里存的令牌
    assert configured.get(config.section_key("ltx", "jwt")) == "JWT1"


def test_fetch_all_relogins_once_on_401(configured):
    state = {"n": 0}

    def search(body, headers):
        state["n"] += 1
        if state["n"] == 1:
            return Resp(401, {"detail": "Token 无效"})
        return _items_payload([_item(7)], has_more=False)

    http = FakeHTTP(search=search)
    src = LtxSource(configured, http=http)
    # 先塞一个旧令牌，模拟「改过密码 / 令牌被作废」
    configured.put(config.section_key("ltx", "jwt"), "OLD")

    rows = src.fetch_all()
    assert [r["id"] for r in rows] == [7]
    logins = [c for c in http.calls if c[0].endswith("/users/login")]
    assert len(logins) == 1          # 只重登一次


def test_fetch_all_requires_credentials(db, box):
    src = LtxSource(SettingsRepo(db, box), http=FakeHTTP())
    with pytest.raises(AvdbNotConfigured):
        src.fetch_all()


def test_fetch_all_stops_at_max_pages(configured, monkeypatch):
    monkeypatch.setattr(config, "AVDB_MAX_PAGES", 3)
    http = FakeHTTP(search=_items_payload([_item(1)], has_more=True))
    rows = LtxSource(configured, http=http).fetch_all()
    assert len(rows) == 3            # 每页 1 条 × 上限 3 页


# ---------------------------------------------------------------- 图床代理


def test_cover_proxy_url_wraps_image_host():
    """★ 图床直连返回加密字节（热链保护），必须经 AVdb 的 img-proxy。"""
    url = cover_proxy_url(
        "http://192.168.0.108:8000/",
        "https://tp.spfcas.com/x/covers/r3/a.jpg",
    )
    assert url.startswith("http://192.168.0.108:8000/api/v1/img-proxy/?url=")
    # 源 URL 必须整体编码（含 :// 与 /），否则上游解析不出目标
    assert "https%3A%2F%2Ftp.spfcas.com%2Fx%2Fcovers%2Fr3%2Fa.jpg" in url
    assert "w=1200" in url and "fmt=webp" in url


def test_cover_proxy_url_empty_source():
    assert cover_proxy_url("http://x", "") == ""


def test_image_fetch_bypasses_env_proxy(monkeypatch):
    seen = {}

    class R:
        status_code = 200
        content = b"\xff\xd8\xff\xdbimg"

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return R()

    monkeypatch.setattr(avdb.httpx, "get", fake_get)
    out = image_fetch("https://img/a.jpg", base_url="http://avdb")
    assert out.startswith(b"\xff\xd8")
    assert "img-proxy" in seen["url"]
    # 局域网目标：绝不能被环境变量里的代理劫持
    assert seen["trust_env"] is False


def test_image_fetch_raises_on_bad_status(monkeypatch):
    class R:
        status_code = 502
        content = b""

    monkeypatch.setattr(avdb.httpx, "get", lambda url, **kw: R())
    with pytest.raises(AvdbError, match="封面下载失败"):
        image_fetch("https://img/a.jpg", base_url="http://avdb")


# ---------------------------------------------------------------- 演员（懒加载）
#
# LTX 资源对象里没有演员字段；演员在 javdb 详情里（见 ``avdb.movie_actress``）。
# 这几条钉住三件事：movie id 怎么从 detail_url 里抠、女优怎么从男女混排里
# 挑出来、以及**绝不能把别人的女优挂到这条资源上**。


class FakeGet:
    """按路径分派的假 httpx.Client（只实现 GET）。"""

    def __init__(self, routes):
        self.routes = routes          # path 后缀 → Resp | callable(params)
        self.calls = []               # [(url, params, headers)]
        self.headers = []

    def get(self, url, *, params=None, timeout=None, headers=None):
        self.calls.append((url, params or {}))
        self.headers.append(headers or {})
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                return resp(params or {}) if callable(resp) else resp
        raise AssertionError(f"未预期的 URL：{url}")


def _movie_payload(number="SSIS-157", actors=None, movie_id="mk01"):
    return Resp(200, {
        "code": 0,
        "data": {
            "movie": {
                "id": movie_id,
                "number": number,
                "actors": actors if actors is not None else [
                    {"id": "a1", "name": "三上悠亜", "gender": 0},
                    {"id": "a2", "name": "今井勇太", "gender": 1},
                    {"id": "a3", "name": " ", "gender": 0},
                ],
            }
        },
    })


def test_movie_id_from_url_reads_path_tail():
    assert avdb.movie_id_from_url("https://javdb.com/v/822NKW") == "822NKW"
    assert avdb.movie_id_from_url("https://javdb580.com/v/2mK52X/") == "2mK52X"


def test_movie_id_from_url_rejects_non_movie_urls():
    assert avdb.movie_id_from_url(None) is None
    assert avdb.movie_id_from_url("") is None
    assert avdb.movie_id_from_url("https://javdb.com/v/x") is None      # 太短
    assert avdb.movie_id_from_url("https://javdb.com/actors/eJmE") is None


def test_code_key_normalizes_separators():
    assert avdb.code_key("noskn-095") == "NOSKN095"
    assert avdb.code_key(" NOSKN_095 ") == "NOSKN095"
    assert avdb.code_key(None) == ""


def test_female_actors_drops_male_and_blanks():
    movie = {"actors": [
        {"name": "三上悠亜", "gender": 0},
        {"name": "今井勇太", "gender": 1},
        {"name": "  ", "gender": 0},
        {"name": "三上悠亜", "gender": 0},       # 去重
        "not-a-dict",
    ]}
    assert avdb.female_actors(movie) == ["三上悠亜"]
    assert avdb.female_actors({}) == []


def test_movie_actress_uses_detail_url_directly():
    http = FakeGet({"/api/v1/javdb/movies/mk01": _movie_payload()})
    names, movie_id = avdb.movie_actress(
        "http://avdb", detail_url="https://javdb.com/v/mk01", number="SSIS-157", http=http
    )
    assert (names, movie_id) == ("三上悠亜", "mk01")
    # 有 id 就不搜：只发了一次请求
    assert len(http.calls) == 1


def test_movie_actress_searches_by_number_when_url_missing():
    search = Resp(200, {"code": 0, "data": {"movies": [
        {"id": "other", "number": "SSIS-158"},
        {"id": "mk01", "number": "ssis-157"},
    ]}})
    http = FakeGet({
        "/api/v1/javdb/search": search,
        "/api/v1/javdb/movies/mk01": _movie_payload(),
    })
    names, movie_id = avdb.movie_actress("http://avdb", detail_url=None,
                                        number="SSIS-157", http=http)
    assert (names, movie_id) == ("三上悠亜", "mk01")
    assert http.calls[0][1]["q"] == "SSIS-157"


def test_movie_actress_never_matches_near_miss_number():
    """搜到的是邻近编号（DPMU-020 vs DPMI-020）时必须放弃，不能张冠李戴。"""
    search = Resp(200, {"code": 0, "data": {"movies": [
        {"id": "m9", "number": "DPMU-020"},
    ]}})
    http = FakeGet({"/api/v1/javdb/search": search})
    assert avdb.movie_actress("http://avdb", detail_url=None,
                              number="DPMI-020", http=http) == (None, None)
    assert len(http.calls) == 1               # 没再去取详情


def test_movie_actress_rejects_movie_with_other_number():
    """detail_url 指向的电影编号对不上 → 不显示演员（宁可没有）。"""
    http = FakeGet({"/api/v1/javdb/movies/mk01": _movie_payload(number="OTHER-001")})
    assert avdb.movie_actress("http://avdb", detail_url="https://javdb.com/v/mk01",
                              number="SSIS-157", http=http) == (None, "mk01")


def test_movie_actress_none_when_movie_has_no_actors():
    http = FakeGet({"/api/v1/javdb/movies/mk01": _movie_payload(actors=[])})
    assert avdb.movie_actress("http://avdb", detail_url="https://javdb.com/v/mk01",
                              number="SSIS-157", http=http) == (None, "mk01")


def test_movie_actress_joins_multiple_names():
    payload = _movie_payload(actors=[
        {"name": "葵つかさ", "gender": 0},
        {"name": "桜空もも", "gender": 0},
    ])
    http = FakeGet({"/api/v1/javdb/movies/mk01": payload})
    names, _ = avdb.movie_actress("http://avdb", detail_url="https://javdb.com/v/mk01",
                                  number="SSIS-157", http=http)
    assert names == "葵つかさ、桜空もも"


def test_movie_actress_raises_on_upstream_502():
    """502 必须冒泡（调用方要区分「查不到」和「这次没问到」）。"""
    http = FakeGet({"/api/v1/javdb/movies/mk01": Resp(502, None, text="bad gateway")})
    with pytest.raises(AvdbError, match="HTTP 502"):
        avdb.movie_actress("http://avdb", detail_url="https://javdb.com/v/mk01",
                           number="SSIS-157", http=http)


def test_movie_actress_raises_auth_error_on_401():
    http = FakeGet({"/api/v1/javdb/movies/mk01": Resp(401, None, text="no auth")})
    with pytest.raises(AvdbAuthError):
        avdb.movie_actress("http://avdb", detail_url="https://javdb.com/v/mk01",
                           number="SSIS-157", http=http)


def test_avdb_get_bypasses_env_proxy(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return _movie_payload()

    monkeypatch.setattr(avdb.httpx, "get", fake_get)
    names, _ = avdb.movie_actress("http://avdb", detail_url="https://javdb.com/v/mk01",
                                  number="SSIS-157")
    assert names == "三上悠亜"
    assert seen["trust_env"] is False


def test_movie_actress_sends_bearer_token():
    """``/api/v1/javdb/*`` 实测**要** JWT（OpenAPI 里写的 security: None 是假的）。"""
    http = FakeGet({"/api/v1/javdb/movies/mk01": _movie_payload()})
    avdb.movie_actress("http://avdb", detail_url="https://javdb.com/v/mk01",
                       number="SSIS-157", token="JWT1", http=http)
    assert http.headers[0]["Authorization"] == "Bearer JWT1"


# ---------------------------------------------------------------- LtxSource.token


def _creds(db, box):
    sr = SettingsRepo(db, box)
    sr.put(config.section_key("ltx", "base_url"), "http://avdb")
    sr.put(config.section_key("ltx", "username"), "u")
    sr.put(config.section_key("ltx", "password"), "p")
    return sr


def test_source_token_returns_cached_jwt(db, box):
    sr = _creds(db, box)
    sr.put(avdb.JWT_KEY, "CACHED")
    http = FakeHTTP()
    assert LtxSource(sr, http=http).token() == "CACHED"
    assert http.calls == []          # 有缓存就不登录


def test_source_token_logs_in_and_stores(db, box):
    sr = _creds(db, box)
    assert LtxSource(sr, http=FakeHTTP()).token() == "JWT1"
    assert sr.get(avdb.JWT_KEY) == "JWT1"


def test_source_token_force_relogins(db, box):
    sr = _creds(db, box)
    sr.put(avdb.JWT_KEY, "OLD")
    http = FakeHTTP()
    assert LtxSource(sr, http=http).token(force=True) == "JWT1"
    assert sr.get(avdb.JWT_KEY) == "JWT1"
    assert len(http.calls) == 1      # 只这一次登录


def test_source_token_without_credentials_raises(db, box):
    with pytest.raises(AvdbNotConfigured):
        LtxSource(SettingsRepo(db, box)).token()
