#!/usr/bin/env python3
"""論文新着コレクター

config.toml に書かれた情報源(RSS / arXiv / OpenAlex / CiNii Research / Crossref)から
新着論文を集め、重複を除いて docs/index.html(一覧ページ)と data/papers.json を更新する。
Python 3.11 以上の標準ライブラリだけで動く(追加インストール不要)。

環境変数(GitHub の Secrets に登録して使う。なくても動くが、その情報源は省略される):
  OPENALEX_API_KEY  OpenAlex の無料APIキー
  CINII_APPID       CiNii Research のアプリケーションID
  CONTACT_EMAIL     Crossref / OpenAlex に伝える連絡先メール(任意)
"""
from __future__ import annotations

import datetime as dt
import gzip
import html
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.toml"
DATA_PATH = ROOT / "data" / "papers.json"
PAGE_PATH = ROOT / "docs" / "index.html"
SUMMARY_PATH = ROOT / "data" / "latest_summary.md"

JST = dt.timezone(dt.timedelta(hours=9))
USER_AGENT = "paper-collector/1.0 (personal research alert; +https://github.com)"

GROUPS = {
    "jp": "日本の機関・日本語論文",
    "japan": "日本関連の英語論文",
    "ecm": "計量経済学",
    "global": "経済・金融WP全般",
    "journal": "主要学術誌",
}

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "rss1": "http://purl.org/rss/1.0/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "arxiv": "http://arxiv.org/schemas/atom",
}


# ---------------------------------------------------------------------------
# 通信
# ---------------------------------------------------------------------------
def http_get(url: str, *, accept: str = "*/*", timeout: int = 40, retries: int = 2) -> bytes:
    """URL を取得して bytes を返す。失敗時は例外。"""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": accept, "Accept-Encoding": "gzip"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (400, 401, 403, 404):
                break  # 再試行しても無駄
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{type(last_err).__name__}: {last_err}")


def http_get_json(url: str) -> dict:
    return json.loads(http_get(url, accept="application/json").decode("utf-8"))


# ---------------------------------------------------------------------------
# 文字列ユーティリティ
# ---------------------------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return WS_RE.sub(" ", s).strip()


def norm_title(title: str) -> str:
    t = clean_text(title).lower()
    return re.sub(r"[\W_]+", "", t)


def norm_doi(doi: str | None) -> str:
    if not doi:
        return ""
    d = doi.strip().lower()
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", d)
    return d


def parse_date(s: str | None) -> str:
    """いろいろな日付表記を YYYY-MM-DD にそろえる。分からなければ空文字。"""
    if not s:
        return ""
    s = s.strip()
    m = re.match(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", s)
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3) or 1)
        return f"{y}-{mo:02d}-{d:02d}"
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(s).date().isoformat()
    except Exception:  # noqa: BLE001
        pass
    m = re.match(r"(\d{4})[/年.](\d{1,2})(?:[/月.](\d{1,2}))?", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3) or 1):02d}"
    m = re.match(r"^(\d{4})$", s)
    if m:
        return f"{m.group(1)}-01-01"
    return ""


def is_japanese_text(s: str) -> bool:
    return bool(re.search(r"[぀-ヿ一-鿿]", s or ""))


# ---------------------------------------------------------------------------
# フィード解析(RSS 2.0 / RSS 1.0(RDF) / Atom)
# ---------------------------------------------------------------------------
def _txt(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_feed(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    items: list[dict] = []
    tag = root.tag

    if tag.endswith("}feed") or tag == "feed":  # Atom
        for e in root.findall("atom:entry", NS):
            link = ""
            for l in e.findall("atom:link", NS):
                if l.get("rel", "alternate") == "alternate" and l.get("href"):
                    link = l.get("href")
                    break
            doi = _txt(e.find("arxiv:doi", NS))
            items.append({
                "title": _txt(e.find("atom:title", NS)),
                "link": link or _txt(e.find("atom:id", NS)),
                "date": _txt(e.find("atom:published", NS)) or _txt(e.find("atom:updated", NS)),
                "authors": [_txt(a.find("atom:name", NS)) for a in e.findall("atom:author", NS)],
                "summary": _txt(e.find("atom:summary", NS)) or _txt(e.find("atom:content", NS)),
                "tags": [c.get("term", "") for c in e.findall("atom:category", NS)],
                "doi": doi,
                "announce": "",
            })
        return items

    # RSS 2.0 は channel/item、RSS 1.0 は rdf:RDF 直下の item(名前空間付き)
    entries = root.findall("./channel/item") + root.findall("rss1:item", NS)
    for e in entries:
        def f(name: str) -> ET.Element | None:
            el = e.find(name)
            if el is None:
                el = e.find(f"rss1:{name}", NS)
            return el

        creators = [_txt(c) for c in e.findall("dc:creator", NS) if _txt(c)]
        if len(creators) == 1 and "," in creators[0] and " " in creators[0] and ";" not in creators[0]:
            # arXiv の RSS は「A, B, C」と1要素にまとめて入っている
            parts = [p.strip() for p in creators[0].split(",")]
            if all(len(p.split()) >= 2 for p in parts):
                creators = parts
        author_el = f("author")
        if not creators and author_el is not None:
            creators = [_txt(author_el)]
        date = _txt(f("pubDate")) or _txt(e.find("dc:date", NS))
        summary = _txt(e.find("content:encoded", NS)) or _txt(f("description"))
        tags = [_txt(s) for s in e.findall("dc:subject", NS)] + [_txt(c) for c in e.findall("category")]
        items.append({
            "title": _txt(f("title")),
            "link": _txt(f("link")) or e.get(f"{{{NS['rdf']}}}about", ""),
            "date": date,
            "authors": creators,
            "summary": summary,
            "tags": [t for t in tags if t],
            "doi": "",
            "announce": _txt(e.find("arxiv:announce_type", NS)),
        })
    return items


# ---------------------------------------------------------------------------
# 各情報源の取得
# ---------------------------------------------------------------------------
def make_record(*, title, url, source, group, authors=None, date="", abstract="", doi="",
                venue="", countries=None, tags=None) -> dict:
    return {
        "title": clean_text(title),
        "url": url or (f"https://doi.org/{norm_doi(doi)}" if doi else ""),
        "authors": [clean_text(a) for a in (authors or []) if clean_text(a)],
        "date": parse_date(date),
        "abstract": clean_text(abstract),
        "doi": norm_doi(doi),
        "venue": clean_text(venue),
        "sources": [source],
        "groups": [group],
        "countries": sorted(set(countries or [])),
        "tags": [clean_text(t) for t in (tags or []) if clean_text(t)][:8],
    }


def fetch_rss(src: dict, ctx: dict) -> list[dict]:
    items = parse_feed(http_get(src["url"], accept="application/rss+xml, application/xml, text/xml"))
    inc_title = re.compile(src["include_title"], re.I) if src.get("include_title") else None
    inc_tags = re.compile(src["include_tags"], re.I) if src.get("include_tags") else None
    out = []
    for it in items:
        if not it["title"]:
            continue
        if inc_title and not inc_title.search(it["title"]):
            continue
        if inc_tags and not any(inc_tags.search(t) for t in it["tags"]):
            continue
        if it["announce"] in ("replace", "replace-cross"):
            continue
        summary = it["summary"]
        # RIETI は description に著者名が入っている
        authors = it["authors"]
        if not authors and "rieti.go.jp" in src["url"] and summary:
            authors = [a.strip() for a in summary.split("／")]
            summary = ""
        link = re.sub(r"\?ref=rss$", "", it["link"])
        out.append(make_record(
            title=it["title"], url=link, source=src["name"], group=src["group"],
            authors=authors, date=it["date"], abstract=summary, doi=it["doi"],
            venue=src["name"], tags=it["tags"],
        ))
    return out


def fetch_arxiv(src: dict, ctx: dict) -> list[dict]:
    q = urllib.parse.urlencode({
        "search_query": f"cat:{src['category']}",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": int(src.get("max_results", 100)),
    })
    items = parse_feed(http_get(f"https://export.arxiv.org/api/query?{q}"))
    time.sleep(3)  # arXiv の利用規約: 3秒に1回まで
    since = ctx["since"]
    out = []
    for it in items:
        d = parse_date(it["date"])
        if d and d < since:
            continue
        link = re.sub(r"v\d+$", "", it["link"])
        out.append(make_record(
            title=it["title"], url=link, source=src["name"], group=src["group"],
            authors=it["authors"], date=d, abstract=it["summary"], doi=it["doi"],
            venue=f"arXiv {src['category']}", tags=it["tags"],
        ))
    return out


def openalex_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    pos: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            pos.append((i, word))
    return " ".join(w for _, w in sorted(pos))


def fetch_openalex(src: dict, ctx: dict) -> list[dict]:
    params = {
        "filter": f"{src['filter']},from_publication_date:{ctx['since']},to_publication_date:{ctx['today']}",
        "per_page": "100",
        "sort": "publication_date:desc",
        "select": "id,doi,title,display_name,authorships,publication_date,primary_location,abstract_inverted_index,type",
    }
    if ctx.get("openalex_key"):
        params["api_key"] = ctx["openalex_key"]
    if ctx.get("email"):
        params["mailto"] = ctx["email"]
    out = []
    cursor = "*"
    for _ in range(int(src.get("max_pages", 2))):
        params["cursor"] = cursor
        data = http_get_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
        for w in data.get("results", []):
            title = w.get("title") or w.get("display_name") or ""
            if not title:
                continue
            authors, countries = [], []
            for a in w.get("authorships", []) or []:
                name = (a.get("author") or {}).get("display_name")
                if name:
                    authors.append(name)
                countries += [c.upper() for c in (a.get("countries") or [])]
            loc = w.get("primary_location") or {}
            venue = ((loc.get("source") or {}).get("display_name")) or ""
            url = loc.get("landing_page_url") or (w.get("doi") or "") or w.get("id", "")
            out.append(make_record(
                title=title, url=url, source=src["name"], group=src["group"],
                authors=authors, date=w.get("publication_date", ""),
                abstract=openalex_abstract(w.get("abstract_inverted_index")),
                doi=w.get("doi") or "", venue=venue, countries=countries,
            ))
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(1)
    return out


def _cinii_value(v) -> str:
    if isinstance(v, list):
        return "; ".join(_cinii_value(x) for x in v)
    if isinstance(v, dict):
        return str(v.get("@value") or v.get("@id") or v.get("name") or "")
    return str(v or "")


def fetch_cinii(src: dict, ctx: dict) -> list[dict]:
    if not ctx.get("cinii_appid"):
        raise RuntimeError("CINII_APPID が未設定のため省略しました(手順書のステップ5参照)")
    since = dt.date.fromisoformat(ctx["since"])
    params = {
        "appid": ctx["cinii_appid"],
        "q": src["q"],
        "format": "json",
        "sortorder": "0",  # 出版年の新しい順
        "count": "100",
        "from": f"{since.year}{since.month:02d}",
    }
    data = http_get_json("https://cir.nii.ac.jp/opensearch/v2/articles?" + urllib.parse.urlencode(params))
    time.sleep(1)
    out = []
    for it in data.get("items", []) or []:
        title = _cinii_value(it.get("title"))
        if not title:
            continue
        link = it.get("link") or {}
        url = link.get("@id") if isinstance(link, dict) else str(link)
        url = url or it.get("@id", "")
        creators = it.get("dc:creator") or []
        if not isinstance(creators, list):
            creators = [creators]
        doi = ""
        for ident in (it.get("dc:identifier") or []) if isinstance(it.get("dc:identifier"), list) else [it.get("dc:identifier")]:
            if isinstance(ident, dict) and "doi" in str(ident.get("@type", "")).lower():
                doi = ident.get("@value", "")
        out.append(make_record(
            title=title, url=url, source=src["name"], group=src["group"],
            authors=[_cinii_value(c) for c in creators],
            date=_cinii_value(it.get("prism:publicationDate")),
            abstract=_cinii_value(it.get("description")), doi=doi,
            venue=_cinii_value(it.get("prism:publicationName")) or _cinii_value(it.get("dc:publisher")),
        ))
    return out


def fetch_journal(src: dict, ctx: dict) -> list[dict]:
    params = {
        "filter": f"from-created-date:{ctx['since']},type:journal-article",
        "rows": "100",
        "sort": "created",
        "order": "desc",
        "select": "DOI,title,author,abstract,created,published,container-title,URL",
    }
    if ctx.get("email"):
        params["mailto"] = ctx["email"]
    url = f"https://api.crossref.org/journals/{src['issn']}/works?" + urllib.parse.urlencode(params)
    data = http_get_json(url)
    time.sleep(1)
    out = []
    for w in (data.get("message") or {}).get("items", []):
        title = " ".join(w.get("title") or [])
        if not title:
            continue
        authors = [" ".join(x for x in (a.get("given"), a.get("family")) if x) for a in w.get("author", []) or []]
        pub = w.get("published") or w.get("created") or {}
        parts = (pub.get("date-parts") or [[None]])[0]
        date = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts) if p) if parts and parts[0] else ""
        out.append(make_record(
            title=title, url=w.get("URL", ""), source=src["name"], group="journal",
            authors=authors, date=date, abstract=w.get("abstract", ""), doi=w.get("DOI", ""),
            venue=" ".join(w.get("container-title") or []) or src["name"],
        ))
    return out


FETCHERS = {
    "rss": fetch_rss,
    "arxiv": fetch_arxiv,
    "openalex": fetch_openalex,
    "cinii": fetch_cinii,
    "journal": fetch_journal,
}


# ---------------------------------------------------------------------------
# 判定・統合
# ---------------------------------------------------------------------------
def add_flags(rec: dict, cfg: dict) -> None:
    fl = cfg.get("flags", {})
    jp_re = re.compile(fl.get("japan_pattern", "japan"), re.I)
    ecm_t = re.compile(fl.get("econometrics_title_pattern", "econometric"), re.I)
    ecm_a = re.compile(fl.get("econometrics_abstract_pattern", "econometric"), re.I)
    text = f"{rec['title']} {rec['abstract']} {' '.join(rec['tags'])}"
    rec["japan"] = bool(
        "jp" in rec["groups"]
        or "JP" in rec["countries"]
        or jp_re.search(text)
        or is_japanese_text(rec["title"])
    )
    hits = {m.group(0).lower() for m in ecm_a.finditer(rec["abstract"])}
    rec["econometrics"] = bool("ecm" in rec["groups"] or ecm_t.search(rec["title"]) or len(hits) >= 2)
    rec["lang"] = "ja" if is_japanese_text(rec["title"]) else "en"


def record_key(rec: dict) -> str:
    if rec["doi"]:
        return "doi:" + rec["doi"]
    # arXiv はIDで同一判定
    m = re.search(r"arxiv\.org/abs/([\w.\-/]+)", rec["url"])
    if m:
        return "arxiv:" + m.group(1)
    return "t:" + norm_title(rec["title"])[:200]


def merge(existing: dict, new: dict) -> None:
    for k in ("sources", "groups", "countries", "tags"):
        existing[k] = sorted(set(existing.get(k, [])) | set(new.get(k, [])))
    for k in ("abstract", "doi", "venue", "date", "url"):
        if not existing.get(k) and new.get(k):
            existing[k] = new[k]
    if len(new.get("authors", [])) > len(existing.get("authors", [])):
        existing["authors"] = new["authors"]


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------
def build_summary(cfg: dict, new_items: list[dict], run: dict, page_url: str) -> str:
    lines = [f"# {cfg['general'].get('title', '論文新着')} {run['date']}", ""]
    lines.append(f"今回の新着は **{len(new_items)} 本** です。")
    if page_url:
        lines.append(f"一覧ページ(絞り込み・検索ができます): {page_url}")
    lines.append("")
    jp_ecm = [r for r in new_items if r["japan"] and r["econometrics"]]
    if jp_ecm:
        lines.append(f"## ★ 日本関連 × 計量 ({len(jp_ecm)})")
        for r in jp_ecm[:20]:
            lines.append(f"- [{r['title']}]({r['url']}) — {r['venue'] or r['sources'][0]}")
        lines.append("")
    for g, label in GROUPS.items():
        rs = [r for r in new_items if g in r["groups"]]
        if not rs:
            continue
        lines.append(f"## {label} ({len(rs)})")
        for r in rs[:15]:
            lines.append(f"- [{r['title']}]({r['url']})")
        if len(rs) > 15:
            lines.append(f"- …ほか {len(rs) - 15} 本は一覧ページで")
        lines.append("")
    if run["errors"]:
        lines.append("## 取得できなかった情報源")
        for name, msg in run["errors"].items():
            lines.append(f"- {name}: {msg[:160]}")
        lines.append("")
    return "\n".join(lines)


def render_page(cfg: dict, papers: list[dict], runs: list[dict]) -> str:
    template = (ROOT / "page_template.html").read_text(encoding="utf-8")
    payload = {
        "title": cfg["general"].get("title", "論文新着コレクター"),
        "groups": GROUPS,
        "papers": papers,
        "runs": runs[-12:],
    }
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return (template
            .replace("__TITLE__", html.escape(payload["title"]))
            .replace("__DATA__", data_json))


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def main() -> int:
    cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    gen = cfg.get("general", {})
    now = dt.datetime.now(JST)
    today = now.date()
    since = today - dt.timedelta(days=int(gen.get("lookback_days", 21)))
    ctx = {
        "today": today.isoformat(),
        "since": since.isoformat(),
        "openalex_key": os.environ.get("OPENALEX_API_KEY", "").strip(),
        "cinii_appid": os.environ.get("CINII_APPID", "").strip(),
        "email": os.environ.get("CONTACT_EMAIL", "").strip(),
    }
    page_url = os.environ.get("PAGE_URL", "").strip()
    max_abs = int(gen.get("abstract_max_chars", 700))

    store = {"papers": {}, "runs": []}
    if DATA_PATH.exists():
        store = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    papers: dict = store.get("papers", {})
    # 既存のキーをタイトルでも引けるようにしておく(DOIの有無が後から変わる場合の重複対策)
    title_index = {"t:" + norm_title(p["title"])[:200]: k for k, p in papers.items()}

    run = {"date": now.strftime("%Y-%m-%d %H:%M"), "counts": {}, "errors": {}, "new": 0}
    new_keys: list[str] = []

    for kind, fetcher in FETCHERS.items():
        for src in cfg.get(kind, []):
            if not src.get("enabled", True):
                continue
            src.setdefault("group", "journal" if kind == "journal" else "global")
            name = src["name"]
            try:
                recs = fetcher(src, ctx)
                run["counts"][name] = len(recs)
                print(f"[ok]   {name}: {len(recs)} 件", flush=True)
            except Exception as e:  # noqa: BLE001
                run["errors"][name] = str(e)
                print(f"[skip] {name}: {e}", flush=True)
                continue
            for rec in recs:
                if not rec["title"]:
                    continue
                rec["abstract"] = rec["abstract"][:max_abs]
                key = record_key(rec)
                tkey = "t:" + norm_title(rec["title"])[:200]
                existing_key = key if key in papers else title_index.get(tkey)
                if existing_key:
                    merge(papers[existing_key], rec)
                    add_flags(papers[existing_key], cfg)
                    continue
                rec["first_seen"] = today.isoformat()
                add_flags(rec, cfg)
                papers[key] = rec
                title_index[tkey] = key
                new_keys.append(key)

    # 古いものを削除
    cutoff = (today - dt.timedelta(days=int(gen.get("keep_days", 90)))).isoformat()
    papers = {k: p for k, p in papers.items() if p.get("first_seen", "") >= cutoff}

    run["new"] = len(new_keys)
    runs = (store.get("runs", []) + [run])[-30:]
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps({"papers": papers, "runs": runs}, ensure_ascii=False, indent=1), encoding="utf-8")

    ordered = sorted(papers.values(), key=lambda p: (p.get("first_seen", ""), p.get("date", "")), reverse=True)
    PAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PAGE_PATH.write_text(render_page(cfg, ordered, runs), encoding="utf-8")

    new_items = [papers[k] for k in new_keys if k in papers]
    SUMMARY_PATH.write_text(build_summary(cfg, new_items, run, page_url), encoding="utf-8")

    print(f"\n新着 {len(new_items)} 本 / 保存中 {len(papers)} 本 / 取得失敗 {len(run['errors'])} 件")
    # GitHub Actions 用に新着件数を出力
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"new_count={len(new_items)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
