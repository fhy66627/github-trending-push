# Requires: Python >= 3.9  (uses asyncio.to_thread)
# Deps: aiohttp, beautifulsoup4, deep-translator
from __future__ import annotations  # 让 list[dict] 等注解惰性求值

import asyncio
import smtplib
import os
import sys
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime
from urllib.parse import quote

import aiohttp
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    # 强制英文页面，避免按 IP 返回中文导致 "No description" 判断失效
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------- 翻译 ----------
def translate_to_zh(text: str) -> str:
    """把英文翻译成中文，失败返回空串（由调用方决定是否回退原文）。"""
    if not text:
        return ""
    try:
        # 每次新建实例：deep-translator 每次 translate() 内部都会重建 session，
        # 复用实例收益≈0，但多线程共享内部状态有竞争风险。
        translator = GoogleTranslator(source="auto", target="zh-CN")
        return translator.translate(text) or ""
    except Exception as e:
        print(f"[翻译失败] {text[:40]}... 原因: {e}")
        return ""


async def translate_async(text: str, sem: asyncio.Semaphore) -> str:
    """翻译库是同步阻塞的，放到线程池执行；用 sem 限流避免被 Google 429。"""
    async with sem:
        return await asyncio.to_thread(translate_to_zh, text)


# ---------- 抓取 + 解析 ----------
async def fetch_github_trending(language: str = "", since: str = "daily") -> list[dict]:
    # 防御性兜底：main() 已校正过，这里再保一次以防被单独调用
    if since not in {"daily", "weekly", "monthly"}:
        since = "daily"

    url = "https://github.com/trending"
    if language:
        # GitHub Trending 的语言 slug 用连字符，不是 %20
        # "jupyter notebook" -> "jupyter-notebook"
        slug = language.strip().lower().replace(" ", "-")
        # safe 保留 '+'：GitHub Trending 的 C++ 语言页 URL 字面就是 /trending/c++，
        # 若 quote 把 '+' 编码成 %2B 会 404。其他常见语言 slug 均为纯 ASCII，不受影响。
        url += f"/{quote(slug, safe='+')}"
    url += f"?since={since}"

    timeout = aiohttp.ClientTimeout(total=30)
    html = ""  # 提前初始化，防御未来重构

    # 每次重试新建 session，避免复用失效的 TLS/HTTP2 连接
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(headers=HEADERS) as session:
                async with session.get(url, timeout=timeout) as resp:
                    resp.raise_for_status()
                    html = await resp.text()
                    break
        except Exception as e:
            print(f"[抓取失败] 第 {attempt + 1} 次: {e}")
            if attempt == 2:
                raise
            await asyncio.sleep(2)

    soup = BeautifulSoup(html, "html.parser")
    repo_list = soup.find_all("article", class_="Box-row")
    results = []

    for repo in repo_list[:10]:
        title = repo.find("h2", class_="h3")
        if not title:
            continue
        repo_link = title.find("a")
        if not repo_link:
            continue

        href = repo_link.get("href", "")
        if not href:           # 残缺条目（缺 href）不收录，避免邮件里出现裸域名
            continue

        name = repo_link.get_text(strip=True).replace(" ", "")
        repo_url = "https://github.com" + href

        # 描述
        desc_tag = repo.find("p", class_="col-9")
        description_en = desc_tag.get_text(strip=True) if desc_tag else ""
        if description_en.lower().startswith("no description"):
            description_en = ""

        # 底部信息栏：语言 / stars / forks
        lang = ""
        stars = ""
        forks = ""

        meta = repo.find("div", class_="f6")
        if meta:
            lang_tag = meta.find("span", itemprop="programmingLanguage")
            if lang_tag:
                lang = lang_tag.get_text(strip=True)

            star_tag = meta.find("a", href=lambda h: h and h.endswith("/stargazers"))
            if star_tag:
                stars = star_tag.get_text(strip=True)

            fork_tag = meta.find("a", href=lambda h: h and h.endswith("/forks"))
            if fork_tag:
                forks = fork_tag.get_text(strip=True)

        results.append({
            "name": name,
            "url": repo_url,
            "description_en": description_en,
            "language": lang,
            "stars": stars,
            "forks": forks,
        })

    return results


# ---------- 组装邮件正文 ----------
async def build_report(repos: list[dict], since: str, sem: asyncio.Semaphore) -> str:
    time_map = {"daily": "今日", "weekly": "本周", "monthly": "本月"}
    label = time_map.get(since, since)

    lines = [f"📅 GitHub 热门项目（{label} Top {len(repos)}）", ""]

    # 并发翻译所有描述
    translate_tasks = [
        translate_async(r["description_en"], sem) if r["description_en"]
        else asyncio.sleep(0, result="")
        for r in repos
    ]
    # return_exceptions=True：单个任务炸了不影响整体
    raw_translations = await asyncio.gather(*translate_tasks, return_exceptions=True)
    translations = [t if isinstance(t, str) else "" for t in raw_translations]

    for i, (repo, zh_desc) in enumerate(zip(repos, translations), 1):
        lines.append(f"{i}. {repo['name']}")
        if zh_desc:
            lines.append(f"   📖 简介：{zh_desc}")
        if repo["description_en"] and repo["description_en"] != zh_desc:
            lines.append(f"   📝 原文：{repo['description_en']}")
        lines.append(f"   🔗 链接：{repo['url']}")
        if repo["language"]:
            lines.append(f"   🛠️ 语言：{repo['language']}")
        if repo["stars"]:
            lines.append(f"   ⭐ 星标：{repo['stars']}")
        if repo["forks"]:
            lines.append(f"   🍴 分叉：{repo['forks']}")
        lines.append("")

    return "\n".join(lines)


# ---------- 发邮件 ----------
def send_email(subject: str, content: str):
    # 用 `or` 兜底：防止 CI 里把环境变量显式设为 "" 时拿到空字符串
    sender_email = os.environ.get("SENDER_EMAIL") or "3329401139@qq.com"
    sender_password = os.environ.get("SENDER_PASSWORD") or ""
    receiver_email = os.environ.get("RECEIVER_EMAIL") or "3329401139@qq.com"

    if not sender_password:
        raise RuntimeError("缺少 SENDER_PASSWORD 环境变量（QQ 邮箱 SMTP 授权码）")

    to_addrs = [x.strip() for x in receiver_email.split(",") if x.strip()]
    if not to_addrs:
        raise RuntimeError("收件人列表为空，请检查 RECEIVER_EMAIL")

    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = sender_email
    msg["To"] = ", ".join(to_addrs)

    # QQ 邮箱推荐 465 + SSL，587+STARTTLS 有时会被拒
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as server:
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, to_addrs, msg.as_string())

    print("邮件发送成功")


# ---------- 主流程 ----------
async def main():
    # sem 的生命周期与 event loop 严格对齐，跑多次 main() 互不干扰
    sem = asyncio.Semaphore(3)

    language = os.environ.get("TRENDING_LANGUAGE") or ""   # 例如 python / go
    since = os.environ.get("TRENDING_SINCE") or "daily"    # daily / weekly / monthly
    # 在入口处统一校正，保证后续 build_report 标题与抓取口径一致
    if since not in {"daily", "weekly", "monthly"}:
        print(f"[警告] 无效的 TRENDING_SINCE={since!r}，已回退为 daily")
        since = "daily"

    print(f"[{datetime.now()}] 开始抓取 GitHub Trending ...")

    try:
        repos = await fetch_github_trending(language=language, since=since)
    except Exception as e:
        print(f"抓取失败：{e}")
        sys.exit(1)

    if not repos:
        print("未获取到任何项目，可能页面结构变化或被限流。")
        sys.exit(1)

    body = await build_report(repos, since, sem)

    date_str = datetime.now().strftime("%Y年%m月%d日")
    subject = f"📅 GitHub 热门项目推送 - {date_str}"
    email_content = (
        f"{body}\n"
        f"—— 发送时间：{datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}"
    )

    try:
        await asyncio.to_thread(send_email, subject, email_content)
    except Exception as e:
        print(f"邮件发送失败：{e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())