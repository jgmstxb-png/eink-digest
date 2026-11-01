#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
四道闸门 —— 每天从订阅源里筛出 N 篇，生成一个 RSS 文件给墨水屏阅读器订阅。
只用标准库，可直接在 GitHub Actions 上跑。

闸门：
  1. 时效    只留 LAST_HOURS 小时内发布
  2. 去重    链接 / 规范化标题 / 标题前 14 字相似都算重复
  3. 字数    正文（去标签后）少于 MIN_CHARS 字丢弃
  4. 配额    每个分类按 quota 上限取，且按来源轮转，避免单一站点刷屏
"""
import json
import os
import re
import sys
import time
import html
import hashlib
import urllib.request
import urllib.error
import concurrent.futures as cf
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime, parsedate_to_datetime

try:
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "docs")
ARCHIVE_DIR = os.path.join(OUT_DIR, "archive")

LAST_HOURS = 36          # 给时区/发布延迟留余量
MIN_CHARS = 400          # 第三道闸门（默认）
MIN_CHARS_BY_CAT = {"auto": 180, "science": 180}  # 英文源摘要短，单独放宽
FETCH_ARTICLE_MAX = 60   # 最多补抓多少篇正文（避免被限速）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>", "\n", s)
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return WS_RE.sub(" ", s).strip()


def text_len(s):
    """中文按字计，英文按词计，混排取较大值，避免英文源被误杀。"""
    cjk = len(CJK_RE.findall(s))
    words = len(re.findall(r"[A-Za-z]{2,}", s))
    return max(cjk, words)


def http_get(url, timeout=15, limit=800_000, retries=3):
    last = None
    for i in range(retries):
        try:
            return _http_get_once(url, timeout, limit)
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def _http_get_once(url, timeout=15, limit=800_000):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml,application/xml,text/xml,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit).decode("utf-8", "ignore")


def tag_text(block, names):
    for n in names:
        m = re.search(rf"(?is)<{n}[^>]*>(.*?)</{n}>", block)
        if m:
            v = m.group(1)
            v = re.sub(r"(?s)^<!\[CDATA\[(.*?)\]\]>$", r"\1", v.strip())
            return v.strip()
    return ""


def attr(block, tag, name):
    m = re.search(rf"""(?is)<{tag}[^>]*\b{name}\s*=\s*["']([^"']+)["']""", block)
    return m.group(1).strip() if m else ""


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s.replace("Z", "+0000") if fmt.endswith("%z") else s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def parse_feed(xml):
    """兼容 RSS 2.0 / Atom / RDF"""
    items = []
    blocks = re.findall(r"(?is)<item[^>]*>(.*?)</item>", xml)
    if blocks:
        for b in blocks:
            link = (tag_text(b, ["link"]) or attr(b, "link", "href")
                    or strip_html(tag_text(b, ["guid", "id"])))
            items.append({
                "title": strip_html(tag_text(b, ["title"])),
                "link": link,
                "desc": tag_text(b, ["content:encoded", "description", "summary"]),
                "date": parse_date(tag_text(b, ["pubDate", "dc:date", "published", "updated"])),
            })
        return items
    for b in re.findall(r"(?is)<entry[^>]*>(.*?)</entry>", xml):
        link = attr(b, "link", "href")
        if not link:
            m = re.search(r"(?is)<link[^>]*>(.*?)</link>", b)
            link = m.group(1).strip() if m else ""
        items.append({
            "title": strip_html(tag_text(b, ["title"])),
            "link": link,
            "desc": tag_text(b, ["content", "summary"]),
            "date": parse_date(tag_text(b, ["published", "updated"])),
        })
    return items


def norm_title(t):
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t or "")
    return t.lower()


def fetch_one(feed, cat_key, cutoff):
    name, url = feed["name"], feed["url"]
    try:
        xml = http_get(url)
    except Exception as e:
        return {"feed": name, "cat": cat_key, "items": [], "error": str(e)[:80]}
    out = []
    for it in parse_feed(xml):
        if not it["title"] or len(it["title"]) < 6 or not it["link"]:
            continue
        if not it["date"] or it["date"] < cutoff:      # 闸门 1
            continue
        out.append({
            "feed": name,
            "cat": cat_key,
            "title": it["title"],
            "link": it["link"],
            "date": it["date"],
            "body": strip_html(it["desc"]),
            "chars": text_len(strip_html(it["desc"])),
        })
    return {"feed": name, "cat": cat_key, "items": out, "error": None}


def enrich(item):
    """闸门 3：正文太短就去抓原文补字数；抓不到就用摘要字数。"""
    if item["chars"] >= MIN_CHARS:
        return item
    try:
        page = http_get(item["link"], timeout=12, limit=400_000)
        body = strip_html(page)
        item["chars_page"] = text_len(body)
    except Exception:
        item["chars_page"] = 0
    return item


def pick(cands, quota, label):
    """闸门 4：按分类配额取，同源轮转，避免一家占满。"""
    if quota <= 0 or not cands:
        return []
    cands = sorted(cands, key=lambda x: x["date"], reverse=True)
    by_feed = {}
    for c in cands:
        by_feed.setdefault(c["feed"], []).append(c)
    feeds = list(by_feed.keys())
    order = {f: i for i, f in enumerate(feeds)}
    picked, seen_idx = [], {}
    while len(picked) < quota:
        progressed = False
        for f in feeds:
            i = seen_idx.get(f, 0)
            if i < len(by_feed[f]):
                picked.append(by_feed[f][i])
                seen_idx[f] = i + 1
                progressed = True
                if len(picked) >= quota:
                    break
        if not progressed:
            break
    picked.sort(key=lambda x: x["date"], reverse=True)
    print(f"  [{label}] 候选 {len(cands)} → 取 {len(picked)}/{quota}")
    return picked


HTML_TPL = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日精选 · {date}</title>
<style>
  :root{{ --fs: 20px; }}
  html{{background:#fff;color:#111}}
  body{{max-width:42rem;margin:0 auto;padding:1.2rem 1.1rem 4rem;
       font:var(--fs)/1.75 -apple-system,"HarmonyOS Sans SC","Noto Sans CJK SC","Microsoft YaHei",sans-serif}}
  h1{{font-size:1.35em;margin:.2rem 0 .1rem}}
  .sub{{color:#666;font-size:.78em;margin:0 0 1rem}}
  .bar{{position:sticky;top:0;background:#fff;border-bottom:1px solid #ddd;
       padding:.5rem 0;display:flex;gap:.5rem;align-items:center;margin-bottom:1rem}}
  .bar button{{font:inherit;font-size:.8em;padding:.25em .7em;border:1px solid #111;
       background:#fff;color:#111;border-radius:.3em}}
  .bar .sp{{color:#888;font-size:.75em}}
  h2{{font-size:.9em;letter-spacing:.08em;color:#444;border-left:3px solid #111;
      padding-left:.5em;margin:1.6rem 0 .6rem}}
  ol{{list-style:none;padding:0;margin:0}}
  li{{padding:.7rem 0;border-bottom:1px solid #eee}}
  li a{{color:#111;text-decoration:none;font-weight:600;font-size:1em;line-height:1.5}}
  li .m{{display:block;color:#777;font-size:.74em;margin-top:.25rem}}
  footer{{margin-top:2.5rem;color:#999;font-size:.72em}}
</style></head><body>
<h1>每日精选</h1>
<p class="sub">{date} · {count} 篇 · 每天 07:00 自动更新</p>
<div class="bar">
  <button onclick="fs(-1)">A−</button>
  <button onclick="fs(1)">A＋</button>
  <span class="sp" id="hint">字号</span>
</div>
{body}
<footer>四道闸门：时效 36h · 去重 · 正文字数 · 分类配额。摘要只给来源，判断留给原文。</footer>
<script>
var cur = parseInt(localStorage.getItem('fs') || '20');
function apply(){{ document.documentElement.style.setProperty('--fs', cur + 'px');
  document.getElementById('hint').textContent = '字号 ' + cur + 'px'; }}
function fs(d){{ cur = Math.min(34, Math.max(15, cur + d));
  localStorage.setItem('fs', cur); apply(); }}
apply();
</script></body></html>"""


def render_html(chosen, date_str, count):
    """生成手机/电脑浏览器直接读的页面（鸿蒙等装不了 APK 的设备用这个）。"""
    esc = lambda s: html.escape(s or "", quote=True)
    groups = {}
    order = []
    for cat, it in chosen:
        if cat not in groups:
            groups[cat] = []
            order.append(cat)
        groups[cat].append(it)
    out = []
    for cat in order:
        out.append(f"<h2>{esc(cat)}</h2><ol>")
        for it in groups[cat]:
            d = it["date"].astimezone(timezone(timedelta(hours=8)))
            out.append(
                "<li><a href=\"{link}\" target=\"_blank\" rel=\"noopener\">{title}</a>"
                "<span class=\"m\">{feed} · {chars} 字 · {t}</span></li>".format(
                    link=esc(it["link"]), title=esc(it["title"]), feed=esc(it["feed"]),
                    chars=max(it["chars"], it.get("chars_page", 0)),
                    t=d.strftime("%m-%d %H:%M")))
        out.append("</ol>")
    return HTML_TPL.format(date=date_str, count=count, body="\n".join(out))


def main():
    cfg = json.load(open(os.path.join(HERE, "feeds.json"), encoding="utf-8"))
    quota = cfg["quota"]
    labels = cfg["labels"]

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=LAST_HOURS)

    plan = [(f, c["key"]) for c in cfg["categories"] for f in c["feeds"]]
    print(f"源 {len(plan)} 个，时间窗 {LAST_HOURS}h（{cutoff:%Y-%m-%d %H:%M} UTC 之后）")

    results = []
    with cf.ThreadPoolExecutor(10) as ex:
        for r in ex.map(lambda a: fetch_one(*a, cutoff), plan):
            if r["error"]:
                print(f"  ✗ {r['feed']}: {r['error']}")
            else:
                print(f"  ✓ {r['feed']}: {len(r['items'])} 条在时效内")
            results.append(r)

    # 闸门 2：去重
    seen_links, seen_titles, flat = set(), set(), []
    for r in results:
        for it in r["items"]:
            key = it["link"].split("?")[0].rstrip("/")
            nt = norm_title(it["title"])
            if key in seen_links or (nt and nt[:24] in seen_titles):
                continue
            seen_links.add(key)
            seen_titles.add(nt[:24])
            flat.append(it)
    print(f"闸门2 去重后：{len(flat)} 条")

    # 闸门 3：字数
    todo = [x for x in flat if x["chars"] < MIN_CHARS][:FETCH_ARTICLE_MAX]
    if todo:
        print(f"补抓正文 {len(todo)} 篇…")
        with cf.ThreadPoolExecutor(8) as ex:
            list(ex.map(enrich, todo))
    passed = [x for x in flat if max(x["chars"], x.get("chars_page", 0)) >= MIN_CHARS_BY_CAT.get(x["cat"], MIN_CHARS)]
    print(f"闸门3 字数≥{MIN_CHARS}：{len(passed)} 条（砍掉 {len(flat)-len(passed)}）")

    # 闸门 4：配额
    chosen = []
    for key in quota:
        subset = [x for x in passed if x["cat"] == key]
        chosen += [(labels.get(key, key), x) for x in pick(subset, quota[key], labels.get(key, key))]
    chosen.sort(key=lambda p: p[1]["date"], reverse=True)

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    date_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    def esc(s):
        return html.escape(s or "", quote=True)

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        "<title>首长每日精选</title>",
        "<link>https://jgmstxb-png.github.io/eink-digest/today.xml</link>",
        f"<description>四道闸门筛出 {len(chosen)} 篇 · {date_str}</description>",
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>",
        "<language>zh-cn</language>",
    ]
    for cat, it in chosen:
        desc = f"【{cat}】{it['feed']} · {max(it['chars'], it.get('chars_page', 0))} 字"
        parts.append("<item>")
        parts.append(f"<title>{esc(it['title'])}</title>")
        parts.append(f"<link>{esc(it['link'])}</link>")
        parts.append(f'<guid isPermaLink="false">{hashlib.md5(it["link"].encode()).hexdigest()}</guid>')
        parts.append(f"<pubDate>{format_datetime(it['date'])}</pubDate>")
        parts.append(f"<category>{esc(cat)}</category>")
        parts.append(f"<description>{esc(desc)}</description>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    xml = "\n".join(parts)

    open(os.path.join(OUT_DIR, "today.xml"), "w", encoding="utf-8").write(xml)
    open(os.path.join(ARCHIVE_DIR, f"{date_str}.xml"), "w", encoding="utf-8").write(xml)
    open(os.path.join(OUT_DIR, "read.html"), "w", encoding="utf-8").write(
        render_html(chosen, date_str, len(chosen)))

    print(f"\n最终 {len(chosen)} 篇，已写 docs/today.xml + archive/{date_str}.xml")
    for cat, it in chosen:
        print(f"  [{cat}] {it['title'][:38]} — {it['feed']}")
    if len(chosen) < sum(quota.values()):
        print(f"⚠ 未填满配额（目标 {sum(quota.values())}），说明源当天产出不足或时效太严")


if __name__ == "__main__":
    main()
