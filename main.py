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
# OpenAI / Discord クライアント
# =========================
client_openai = OpenAI(api_key=OPENAI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
client = discord.Client(intents=intents)

# Assistants API 用：DiscordスレッドID → OpenAIスレッドID
assistant_threads: dict[int, str] = {}

# Platform で作った「ミス・イーランド（Discord版）」の Assistant ID
# ※ OpenAI Platform の Assistant 詳細画面に表示されている ID を使う
ASSISTANT_ID = "asst_SHERQFWpRYbQdftMRpdbYgyr"


# =========================
# ICI向け 検索クエリ判定ロジック
# =========================

def build_search_query(text: str) -> str | None:
    """
    ICIコミュニティ向け：
    「これはWeb検索した方がよさそうなメッセージか？」を判定し、
    検索クエリを返す。検索不要なら None を返す。
    """

    # メンションや呼びかけをざっくり除去
    cleaned = re.sub(r"<@!?[0-9]+>", "", text)  # Discordメンション削除
    cleaned = cleaned.replace("ミス・イーランド", "").replace("ミスイーランド", "")
    cleaned = cleaned.strip()
    if not cleaned:
        return None

    # --- 0. 明示的な「調べて」「検索して」系 ---
    explicit_search_words = ["調べて", "検索して", "ググって", "探して", "調査して"]
    if any(w in cleaned for w in explicit_search_words):
        q = cleaned
        for w in explicit_search_words:
            q = q.replace(w, "")
        q = q.strip(" ？?、。")
        return q or None

    # --- 1. 時事・ニュース・トレンド系 ---
    time_words = ["今", "現在", "いま", "直近", "最近", "最新", "今年", "今年度", "今日"]
    news_words = ["ニュース", "動向", "トレンド", "情勢", "状況"]
    if any(t in cleaned for t in time_words) and any(n in cleaned for n in news_words):
        # 例：「最近のZ世代の離職ニュースは？」「今の円相場の状況は？」
        return cleaned

    # --- 2. 役職＋「今／現在／いま」系（首相・大統領・社長など） ---
    role_words = [
        "首相", "総理大臣", "総理", "大統領",
        "知事", "市長", "社長", "会長", "CEO",
        "監督", "キャプテン", "代表", "学長", "理事長"
    ]
    if any(r in cleaned for r in role_words) and any(t in cleaned for t in time_words):
        # 例：「日本の首相は今誰ですか？」「今のマイクロソフトのCEOは？」
        return cleaned

    # 「◯◯は誰ですか？」パターン（役職語がなくても一応拾う）
    if cleaned.endswith("誰ですか？") or cleaned.endswith("誰ですか") or cleaned.endswith("誰？"):
        if len(cleaned) >= 6:  # あまりに短いものは除外
            return cleaned

    # --- 3. 統計・データ・経済指標など（時間依存しやすいもの） ---
    data_words = [
        "統計", "データ", "推移", "グラフ",
        "物価", "インフレ", "CPI", "失業率", "有効求人倍率",
        "人口", "平均年収", "賃金", "最低賃金", "円相場", "為替", "株価"
    ]
    if any(dw in cleaned for dw in data_words) and any(t in cleaned for t in time_words + ["推移", "変化"]):
        # 例：「最近の有効求人倍率の推移」「今年の最低賃金の状況」
        return cleaned

    # --- 4. 本・サービスなどの評判／レビュー ---
    if any(w in cleaned for w in ["評判", "口コミ", "レビュー", "評価"]):
        m = re.search(r"『(.+?)』", cleaned)
        if m:
            # 書名やサービス名が『』にあれば強調して投げる
            return f"{m.group(1)} 評判 口コミ レビュー"
        return cleaned

    # --- 5. イベント・セミナー・開催情報など ---
    event_words = ["イベント", "セミナー", "講座", "勉強会", "カンファレンス", "ワークショップ"]
    event_detail_words = ["開催", "日時", "スケジュール", "日程", "場所", "会場", "申し込み", "参加方法"]
    if any(e in cleaned for e in event_words) and any(d in cleaned for d in event_detail_words + time_words):
        # 例：「今年のキャリコン関連セミナーの開催情報」
        return cleaned

    # --- 6. ニュースっぽい一般表現 ---
    if "ニュース" in cleaned and ("について" in cleaned or "教えて" in cleaned):
        return cleaned

    # それ以外はモデルの知識で答える（検索に回さない）
    return None


# =========================
# Brave Search 呼び出し
# =========================

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
# Assistants API での会話
# =========================

async def get_response_with_history(thread_id: int, user_input: str, username: str) -> str:
    """
    Assistants API + File Search で、スレッド単位の会話を維持しながら応答を生成する。
    （妹GPTsと同じナレッジを使う役割）
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

            # 3) Assistant を実行（File Search もここで自動的に効く）
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

    # 3) それ以外のメッセージ（ミス・イーランド宛でないもの）は無視
    return


# =========================
# Bot 起動
# =========================
client.run(DISCORD_TOKEN)
