import asyncio
import smtplib
import os
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime

import aiohttp
from bs4 import BeautifulSoup


async def fetch_github_trending(language: str = "", since: str = "daily") -> str:
    url = "https://github.com/trending"
    if language:
        url += f"/{language}"
    url += f"?since={since}"

    async with aiohttp.ClientSession(headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }) as session:
        async with session.get(url, timeout=30) as resp:
            html = await resp.text()
            soup = BeautifulSoup(html, 'html.parser')

            repo_list = soup.find_all('article', class_='Box-row')
            results = []

            for repo in repo_list[:10]:
                title = repo.find('h2', class_='h3')
                if title:
                    repo_link = title.find('a')
                    if repo_link:
                        name = repo_link.get_text(strip=True)
                        repo_url = "https://github.com" + repo_link['href']

                    desc = repo.find('p', class_='col-9')
                    description = desc.get_text(strip=True) if desc else "无描述"

                    meta = repo.find('div', class_='flex-wrap')
                    stars = ""
                    forks = ""
                    lang = ""
                    if meta:
                        meta_items = meta.find_all('span', class_='d-inline-block')
                        for item in meta_items:
                            text = item.get_text(strip=True)
                            if 'stars' in text:
                                stars = text
                            elif 'forks' in text:
                                forks = text
                            else:
                                lang = text

                    results.append({
                        "name": name,
                        "url": repo_url,
                        "description": description,
                        "stars": stars,
                        "forks": forks,
                        "language": lang
                    })

            if not results:
                return "未找到热门项目"

            time_map = {"daily": "今日", "weekly": "本周", "monthly": "本月"}
            time_label = time_map.get(since, since)

            output = f"📅 GitHub 热门项目 ({time_label}):\n\n"
            for i, repo in enumerate(results, 1):
                output += f"{i}. {repo['name']}\n"
                output += f"   📖 描述: {repo['description']}\n"
                output += f"   🔗 链接: {repo['url']}\n"
                if repo['language']:
                    output += f"   🛠️ 语言: {repo['language']}\n"
                if repo['stars']:
                    output += f"   ⭐ 星标: {repo['stars']}\n"
                if repo['forks']:
                    output += f"   🍴 分叉: {repo['forks']}\n"
                output += "\n"

            return output


def send_email(subject: str, content: str):
    sender_email = os.environ.get("SENDER_EMAIL", "3329401139@qq.com")
    sender_password = os.environ.get("SENDER_PASSWORD", "")
    receiver_email = os.environ.get("RECEIVER_EMAIL", "3329401139@qq.com")

    msg = MIMEText(content, 'plain', 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = sender_email
    msg['To'] = receiver_email

    with smtplib.SMTP("smtp.qq.com", 587) as server:
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, receiver_email, msg.as_string())

    print("邮件发送成功")


async def main():
    print(f"开始获取 GitHub Trending: {datetime.now()}")

    result = await fetch_github_trending()

    if "获取失败" in result or "未找到" in result:
        print(f"获取失败: {result}")
        return

    date_str = datetime.now().strftime("%Y年%m月%d日")
    subject = f"📅 GitHub 热门项目推送 - {date_str}"

    email_content = f"""每日 GitHub Trending 推送

{result}

发送时间: {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}
"""

    send_email(subject, email_content)


if __name__ == "__main__":
    asyncio.run(main())