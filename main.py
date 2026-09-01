"""
校網公告自動化智慧篩選與推播系統
=================================
流程：
1. 讀取 last_checked.json（上次已看過的公告 ID 清單）
2. 透過學校網站的 RSS 訂閱抓取公告列表
3. 找出「新」公告（不在 last_checked.json 裡的）
4. 對每則新公告呼叫 Gemini API，判斷是否符合 USER_CRITERIA
5. 符合的話推播到 LINE 群組
6. 更新 last_checked.json（交給 GitHub Actions 去 commit）

本版針對「臺北市立成功高級中學」設定，直接讀取該校的 RSS 訂閱網址
（例如 https://www.cksh.tp.edu.tw/category/news/feed/ ），
比爬 HTML 頁面穩定，也不用猜 CSS 樣式。
若換成別的學校網站，只要該站也有 RSS（大部分學校網站的頁尾都會有
「RSS」連結），把 TARGET_URL 換成該校 RSS 網址即可直接使用；
若該校沒有 RSS，才需要改回 HTML 爬蟲的寫法。
"""

import os
import re
import sys
import json
import time
from datetime import datetime, timezone

import requests
import feedparser
import google.generativeai as genai

STATE_FILE = "last_checked.json"
GEMINI_MODEL = "gemini-2.5-flash"
REQUEST_TIMEOUT = 15


# ---------------------------------------------------------------------------
# 狀態讀寫
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_ids": [], "updated_at": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 抓取公告列表（透過 RSS）
# ---------------------------------------------------------------------------
def fetch_announcements(url: str) -> list[dict]:
    """
    讀取學校的 RSS 訂閱網址，回傳 [{id, title, link, date}, ...]

    RSS 是網站自己提供的、結構固定的公告清單，比爬 HTML 頁面穩定，
    不需要自己猜 CSS 樣式。每個 <item> 對應一則公告：
        - guid 或 link 當作唯一 id（用來判斷是不是「新」公告）
        - title 是標題
        - published 是發佈時間
    """
    resp = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SchoolNoticeBot/1.0)"},
    )
    resp.raise_for_status()

    feed = feedparser.parse(resp.content)

    announcements = []
    for entry in feed.entries:
        link = getattr(entry, "link", "").strip()
        title = getattr(entry, "title", "").strip()
        uid = getattr(entry, "id", None) or link or title
        date_str = getattr(entry, "published", "") or getattr(entry, "updated", "")

        announcements.append(
            {"id": uid, "title": title, "link": link, "date": date_str}
        )

    return announcements


def filter_new(announcements: list[dict], state: dict) -> list[dict]:
    known_ids = set(state.get("last_ids", []))
    return [a for a in announcements if a["id"] not in known_ids]


# ---------------------------------------------------------------------------
# Gemini 判斷
# ---------------------------------------------------------------------------
def check_with_gemini(announcement: dict, criteria: str, api_key: str) -> dict:
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)

    prompt = f"""你是一個校園公告篩選助手。請根據使用者條件，判斷這則公告是否相關。

使用者條件：{criteria}

公告標題：{announcement['title']}
公告日期：{announcement['date']}

只回傳 JSON，不要任何其他文字或說明，格式如下：
{{"is_match": true 或 false, "reason": "一句話說明理由"}}"""

    response = model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"},
    )
    text = (response.text or "").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {"is_match": False, "reason": "Gemini 回傳格式解析失敗"}


# ---------------------------------------------------------------------------
# LINE 推播
# ---------------------------------------------------------------------------
def send_line_message(token: str, group_id: str, text: str) -> None:
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    payload = {"to": group_id, "messages": [{"type": "text", "text": text[:4900]}]}

    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    if resp.status_code != 200:
        print(f"[LINE 推播失敗] {resp.status_code}: {resp.text}", file=sys.stderr)
    else:
        print("[LINE 推播成功]")


# ---------------------------------------------------------------------------
# 組合彙整訊息
# ---------------------------------------------------------------------------
def build_digest_message(matched_items: list[dict]) -> str:
    """
    把所有符合條件的公告組成一則訊息，格式：

    【校網公告小幫手】2026/09/01
    1.公告名稱
    公告頁面連結
    2.公告名稱
    公告頁面連結
    ⚠️AI工具可能出錯
    重要公告請自行留意校網首頁
    """
    today = datetime.now(timezone.utc).astimezone().strftime("%Y/%m/%d")
    lines = [f"【校網公告小幫手】{today}"]

    for i, item in enumerate(matched_items, start=1):
        lines.append(f"{i}.{item['title']}")
        lines.append(item["link"])

    lines.append("⚠️AI工具可能出錯")
    lines.append("重要公告請自行留意校網首頁")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    target_url = os.environ["TARGET_URL"]
    gemini_key = os.environ["GEMINI_API_KEY"]
    line_token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    line_group = os.environ["LINE_GROUP_ID"]
    criteria = os.environ.get("USER_CRITERIA", "")

    state = load_state()

    try:
        announcements = fetch_announcements(target_url)
    except Exception as e:
        print(f"[爬取失敗] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"抓到 {len(announcements)} 則公告")

    new_items = filter_new(announcements, state)
    print(f"其中 {len(new_items)} 則為新公告")

    matched_items = []
    for item in new_items:
        try:
            result = check_with_gemini(item, criteria, gemini_key)
        except Exception as e:
            print(f"[Gemini 判斷失敗] {item['title']}: {e}", file=sys.stderr)
            continue

        print(f"- {item['title']} -> {result}")

        if result.get("is_match"):
            matched_items.append(item)

    # 有符合條件的公告才發送一則彙整訊息；一次執行只發一則，不逐篇推播
    if matched_items:
        msg = build_digest_message(matched_items)
        send_line_message(line_token, line_group, msg)
    else:
        print("本次沒有符合條件的公告，不推播。")

    # 更新狀態：以「這次抓到的公告清單」作為下次比對基準
    # （校網公告列表頁通常只顯示最近 N 筆，用完整清單取代即可，
    #   不需要無限累加，檔案才不會一直長大）
    state["last_ids"] = [a["id"] for a in announcements]
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    print(f"完成。共 {len(matched_items)} 則公告納入本次推播。")


if __name__ == "__main__":
    main()
