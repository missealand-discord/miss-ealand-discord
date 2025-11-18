import os
import re
import asyncio
import concurrent.futures

import discord
import httpx
from openai import OpenAI

# ====== 環境変数 ======
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY")

# ====== OpenAI クライアント ======
client_openai = OpenAI(api_key=OPENAI_API_KEY)

# ====== Discord クライアント ======
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
client = discord.Client(intents=intents)

# ====== Assistants API 用：DiscordスレッドID → OpenAI Thread ID ======
assistant_threads: dict[int, str] = {}

# ====== Brave 検索の「保留中クエリ」をスレッドごとに保持 ======
pending_search_queries: dict[int, str] = {}

# ====== あなたの Assistant ID（今使っているものに差し替えてください） ======
ASSISTANT_ID = "asst_SHERQFWpRYbQdftMRpdbYgyr"  # ←必ず自分のIDに直す


def generate_system_prompt(username: str) -> str:
    """
    必要であれば Assistant 側の System instruction に補足を渡したいとき用。
    今回は「相手の名前」を伝える程度にとどめる。
    """
    return f"""
相手は「{username}」さんです。
敬意と親しみを込めて、落ち着いた安心感のある話し方で応答してください。
"""


# ====== Brave Search 用 関数 ======
async def web_search_brave(query: str) -> str:
    """
    Brave Search API で Web検索し、簡単なリスト形式で返す。
    """
    if not BRAVE_API_KEY:
        return "検索用のAPIキー（BRAVE_API_KEY）がまだ設定されていないみたいです。管理人さんに確認してもらえますか？"

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
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            r = await http_client.get(url, headers=headers, params=params)
            r.raise_for_status()
        data = r.json()
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


# ====== 検索すべきかどうかを判定する関数 ======
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

    # 0. 明らかに「相談・カウンセリング」系のメッセージは検索に回さない
    counsel_words = [
        "相談に乗って", "相談のって", "相談乗って",
        "相談したい", "相談させて", "相談があります",
        "話を聞いて", "話聞いて", "聞いてほしい",
        "聞いてください", "聞いて下さい",
        "カウンセリング", "悩みを聞いて",
    ]
    if any(w in cleaned for w in counsel_words):
        return None

    # 1. 明示的な「調べて／検索して／ググって」系
    explicit_search_words = ["調べて", "検索して", "ググって", "探して", "調査して"]
    if any(w in cleaned for w in explicit_search_words):
        q = cleaned
        for w in explicit_search_words:
            q = q.replace(w, "")
        q = q.strip(" ？?、。")
        return q or None

    # 2. 「評判」「口コミ」「レビュー」が入っている場合（本・サービスなど）
    if any(w in cleaned for w in ["評判", "口コミ", "レビュー"]):
        m = re.search(r"『(.+?)』", cleaned)
        if m:
            return f"{m.group(1)} 書評 評判 口コミ"
        return cleaned

    # 3. 「最新／最近」と「ニュース」が両方入っている → 時事ニュースと判断
    if "ニュース" in cleaned and ("最新" in cleaned or "最近" in cleaned):
        return cleaned

    # それ以外は検索不要
    return None


# ====== Assistants API で会話履歴つき応答を得る ======
async def get_response_with_history(thread_id: int, user_input: str, username: str) -> str:
    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        try:
            # DiscordスレッドIDごとに OpenAI Thread を1本割り当てる
            if thread_id not in assistant_threads:
                thread = await loop.run_in_executor(
                    pool,
                    lambda: client_openai.beta.threads.create()
                )
                assistant_threads[thread_id] = thread.id

            api_thread_id = assistant_threads[thread_id]

            # ユーザーメッセージを追加（ユーザー名も添える）
            user_message = f"{username}さんからのメッセージ：{user_input}"

            await loop.run_in_executor(
                pool,
                lambda: client_openai.beta.threads.messages.create(
                    thread_id=api_thread_id,
                    role="user",
                    content=user_message,
                )
            )

            # Assistant を実行
            run = await loop.run_in_executor(
                pool,
                lambda: client_openai.beta.threads.runs.create_and_poll(
                    thread_id=api_thread_id,
                    assistant_id=ASSISTANT_ID,
                    # 必要なら instructions で username を補足
                    instructions=generate_system_prompt(username),
                )
            )

            if run.status != "completed":
                return f"すみません、応答の生成に失敗しました（status: {run.status}）。"

            # 最新のアシスタントメッセージを取得
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


# ====== Discordイベント ======
@client.event
async def on_ready():
    print(f"ミス・イーランドBotがログインしました: {client.user}")


@client.event
async def on_message(message: discord.Message):
    # 自分自身には反応しない
    if message.author == client.user:
        return

    username = message.author.display_name
    content = message.content.strip()
    channel_id = message.channel.id

    # ========= ① まず「保留中の検索確認」がないかをチェック =========
    if channel_id in pending_search_queries:
        yes_keywords = ["はい", "お願いします", "おねがい", "うん", "yes", "調べて"]
        no_keywords = ["いいえ", "いえ", "結構です", "だいじょうぶ", "自分で調べます", "no"]

        lowered = content.lower()

        # 「はい」系 → Brave 検索を実行
        if any(k in content for k in yes_keywords) or any(k in lowered for k in ["yes", "y"]):
            query = pending_search_queries.pop(channel_id)
            await message.channel.send("では、ネットでお調べしてみますね。少々お待ちくださいませ🕊")
            result = await web_search_brave(query)
            await message.channel.send(result)
            return

        # 「いいえ」系 → 検索をキャンセルして通常応答
        if any(k in content for k in no_keywords) or "no" in lowered:
            pending_search_queries.pop(channel_id, None)
            await message.channel.send("了解しました。では、手元のナレッジでお話しできる範囲でご一緒に考えていきましょうね🕊")
            # このまま下の通常処理に流す

    # ========= ② スレッド内での会話 =========
    if message.channel.type == discord.ChannelType.public_thread:
        thread_id = channel_id

        # まず「検索候補かどうか」を判定
        search_query = build_search_query(content)

        if search_query:
            # いきなり検索はせず、必ず確認を入れる
            pending_search_queries[thread_id] = search_query
            await message.channel.send(
                "手元のICIナレッジには明確な記載が見当たりません。\n"
                "ネットでお調べしましょうか？（はい / いいえ）"
            )
            return

        # 検索でなければ、通常のミス・イーランド（Assistant）応答
        reply = await get_response_with_history(thread_id, content, username)
        await message.channel.send(reply)
        return

    # ========= ③ 通常チャンネルでメンションされた場合 =========
    if client.user in message.mentions:
        # メンション部分を削る
        user_input = re.sub(rf"<@!?{client.user.id}>", "", content).strip()

        # 会話用スレッドを作成（60分で自動アーカイブ）
        thread = await message.create_thread(
            name=f"{username}さんとの会話",
            auto_archive_duration=60,
        )

        if not user_input:
            await thread.send("はいな、なんか用事あるんか？🫶")
            return

        thread_id = thread.id

        # メンション抜きのテキストで「検索候補かどうか」を判定
        search_query = build_search_query(user_input)

        if search_query:
            pending_search_queries[thread_id] = search_query
            await thread.send(
                "手元のICIナレッジには明確な記載が見当たりません。\n"
                "ネットでお調べしましょうか？（はい / いいえ）"
            )
            return

        # 検索でなければ、通常のミス・イーランド応答
        reply = await get_response_with_history(thread_id, user_input, username)
        await thread.send(reply)
        return


# ====== Bot起動 ======
client.run(DISCORD_TOKEN)
