import discord
from openai import OpenAI
import asyncio
import os
import concurrent.futures

# 環境変数からAPIキーを読み込み
DISCORD_TOKEN = os.environ['DISCORD_TOKEN']
OPENAI_API_KEY = os.environ['OPENAI_API_KEY']

# OpenAIクライアントを初期化
client_openai = OpenAI(api_key=OPENAI_API_KEY)

# Discordの接続設定
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
client = discord.Client(intents=intents)

# 会話履歴をスレッドごとに保持←もう使わない
# thread_histories = {}

# Assistants API 用：DiscordスレッドID → OpenAIスレッドID
assistant_threads = {}

# Platform で作った「ミス・イーランド（Discord版）」の ID
ASSISTANT_ID = "asst_SHERQFWpRYbQdftMRpdbYgyr"  


# ユーザー名を反映したSystem Promptを生成
def generate_system_prompt(username):
    return f"""
    あなたの名前は「ミス・イーランド」です。敬意と知性を持ち合わせたAI秘書です。
    相手は「{username}」さんです。親しみと敬意を込めて呼びかけながら会話してください。
    優しく、丁寧で、ユーモアを交えた対話が得意です。
    クライアントの発言に共感的に耳を傾け、必要に応じて関西弁で元気に励ますこともできます。
    相手の問いに対し、思慮深く、十分な文脈と具体例を含めて回答してください。。
    """

async def get_response_with_history(thread_id, user_input, username):
    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        try:
            # 1) OpenAI 側の Thread を、Discord の thread_id ごとに1本作る／再利用する
            if thread_id not in assistant_threads:
                thread = await loop.run_in_executor(
                    pool,
                    lambda: client_openai.beta.threads.create()
                )
                assistant_threads[thread_id] = thread.id

            api_thread_id = assistant_threads[thread_id]

            # 2) ユーザーメッセージを追加
            #    （ユーザー名はここで渡す。詳しい人格設定はAssistant側のSystem instructionsに任せる）
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





# Discord Bot 起動確認
@client.event
async def on_ready():
    print(f'ミス・イーランドBotがログインしました: {client.user}')

# メッセージ受信時の処理
@client.event
async def on_message(message):
    if message.author == client.user:
        return

    username = message.author.display_name

    # スレッド内での会話
    if message.channel.type == discord.ChannelType.public_thread:
        thread_id = message.channel.id
        reply = await get_response_with_history(thread_id, message.content, username)
        await message.channel.send(reply)
        return

    # 通常チャンネルでメンションされたらスレッドを作成
    if client.user in message.mentions:
        user_input = message.content.replace(f"<@{client.user.id}>", "").strip()

        # スレッド作成（60分自動終了）
        thread = await message.create_thread(
            name=f"{username}さんとの会話",
            auto_archive_duration=60
        )
        if user_input == "":
            await thread.send("はいな、なんか用事あるんか？🫶")
            return

        reply = await get_response_with_history(thread.id, user_input, username)
        await thread.send(reply)

# Bot起動
client.run(DISCORD_TOKEN)


