import os
import re
import asyncio
import concurrent.futures

import discord
from openai import OpenAI
import httpx

# =========================
# 環境変数
# =========================
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY")

# =========================
# Brave Search 関連
# =========================

def build_search_query(text: str) -> str | None:
    """
    メッセージから「これはWeb検索した方がよさそうなとき」の検索クエリを作る。
    検索不要なら None を返す。
    """
    # Discordメンション & ボット名をざっくり除去
    cleaned = re.sub(r"<@!?[0-9]+>", "", text)  # メンション削除
    cleaned = cleaned.replace("ミス・イーランド", "").replace("ミスイーランド", "")
    cleaned = cleaned.strip()

    # 1) 「〜を調べて／検索して」
    if "調べて" in cleaned or "検索して" in cleaned:
        q = cleaned.replace("調べて", "").replace("検索して", "")
        q = q.strip(" ？?、。")
        return q or None

    # 2) 「『○○』の評判は？」系
    if "評判" in cleaned or "口コミ" in cleaned or "レビュー" in cleaned:
        m = re.search(r"『(.+?)』", cleaned)
        if m:
            # 書名が『』で囲まれていたら、それ＋評判系キーワードで検索
            return f"{m.group(1)} 書評 評判 口コミ"
        # 囲みがなくても、とりあえず全文で投げる
        return cleaned

    # 3) ニュース系：「最新」「最近」と「ニュース」が両方入っている
    if "ニュース" in cleaned and ("最新" in cleaned or "最近" in cleaned):
        return cleaned

    # それ以外は検索に回さない
    return None


async def web_search_brave(query: str) -> str:
    """Brave Search API で簡単な要約を作る"""
    if not BRAVE_API_KEY:
        return "まだ検索用のAPIキーが設定されていないみたいです。管理人さんに確認してもらえますか？"

    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": BRAVE_API_KEY,
    }
    params = {
        "q": query,
        "count": 5,
        "country": "jp",
        "lang": "ja",
        "safesearch": "moderate",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers=headers, params=params)
            r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            return "今日はちょっとアクセスが集中しているみたいです。時間をおいてもう一度お願いできますか？"
        return f"検索中にエラーが起きました……（HTTP {e.response.status_code}）"
    except Exception as e:
        return f"検索中にエラーが起きました……（{e}）"

    results = data.get("web", {}).get("results", [])
    if not results:
        return "それらしい情報が見つかりませんでした。キーワードを少し変えてみてもらえますか？"

    lines = []
    for item in results[:3]:
        title = item.get("title", "")
        url_item = item.get("url", "")
        snippet = item.get("description", "")
        lines.append(f"・**{title}**\n  {snippet}\n  {url_item}")

    return "ざっとお調べした結果です：\n" + "\n\n".join(lines)


# =========================
# OpenAI / Discord 設定
# =========================

client_openai = OpenAI(api_key=OPENAI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
client = discord.Client(intents=intents)

# Assistants API 用：DiscordスレッドID → OpenAIスレッドID
assistant_threads: dict[int, str] = {}

# Platform で作った「ミス・イーランド（Discord版）」の ID
ASSISTANT_ID = "asst_SHERQFWpRYbQdftMRpdbYgyr"


async def get_response_with_history(thread_id: int, user_input: str, username: str) -> str:
    """
    Assistants API + File Search で、スレッド単位の会話を維持しながら応答を生成する。
    """
    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        try:
            # 1) OpenAI側 Thread を作成／再利用
            if thread_id not in assistant_threads:
                thread = await loop.run_in_executor(
                    pool,
                    lambda: client_openai.beta.threads.create()
                )
                assistant_threads[thread_id] = thread.id

            api_thread_id = assistant_threads[thread_id]

            # 2) ユーザーメッセージを追加
            user_message = f"{username}さんからのメッセージ：{user_input}"

            await loop.run_in_executor(
                pool,
                lambda: client_openai.beta.threads.messages.create(
                    thread_id=api_thread_id,
                    role="user",
                    content=user_message,
                )
            )

            # 3) Assistant を実行
            run = await loop.run_in_executor(
                pool,
                lambda: client_openai.beta.threads.runs.create_and_poll(
                    thread_id=api_thread_id,
                    assistant_id=ASSISTANT_ID,
                )
            )

            if run.status != "completed":
                return f"すみません、応答の生成に失敗しました（status: {run.status}）。"

            # 4) 最新のアシスタントメッセージを取得
            messages = await loop.run_in_executor(
                pool,
                lambda: client_openai.beta.threads.messages.list(
                    thread_id=api_thread_id,
                    order="desc",
                    limit=5,
                )
            )

            for msg in messages.data:
                if msg.role == "assistant" and msg.content:
                    part = msg.content[0]
                    if hasattr(part, "text"):
                        return part.text.value.strip()

            return "すみません、返信を取得できませんでした。"

        except Exception as e:
            return f"申し訳ありません、ちょっと考えごとしてましたわ……（エラー: {e}）"


# =========================
# Discord イベント
# =========================

@client.event
async def on_ready():
    print(f"ミス・イーランドBotがログインしました: {client.user}")


@client.event
async def on_message(message: discord.Message):
    # Bot（自分を含む）は無視
    if message.author.bot:
        return

    username = message.author.display_name
    content = message.content.strip()

    # ---------- 1) スレッド内での会話 ----------
    if message.channel.type == discord.ChannelType.public_thread:
        search_query = build_search_query(content)

        if search_query:
            await message.channel.send("ちょっとネットで調べてきますね、少々お待ちくださいませ🕊")
            reply = await web_search_brave(search_query)
        else:
            thread_id = message.channel.id
            reply = await get_response_with_history(thread_id, content, username)

        await message.channel.send(reply)
        return

    # ---------- 2) 通常チャンネルで @ミス・イーランド と呼ばれたとき ----------
    if client.user in message.mentions:
        # メンション部分を削る
        user_input = re.sub(rf"<@!?{client.user.id}>", "", content).strip()

        # 会話用スレッドを作成（60分で自動クローズ）
        thread = await message.create_thread(
            name=f"{username}さんとの会話",
            auto_archive_duration=60,
        )

        if not user_input:
            await thread.send("はいな、なんか用事あるんか？🫶")
            return

        search_query = build_search_query(user_input)

        if search_query:
            await thread.send("ちょっとネットで調べてきますね、少々お待ちくださいませ🕊")
            reply = await web_search_brave(search_query)
        else:
            reply = await get_response_with_history(thread.id, user_input, username)

        await thread.send(reply)
        return

    # 3) それ以外のメッセージは無視
    return


# =========================
# Bot 起動
# =========================
client.run(DISCORD_TOKEN)

