from openai import OpenAI
import os

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

print("=== 利用可能なモデル一覧 ===")
models = client.models.list()
for m in models.data:
    print(m.id)
print("=== 完了 ===")
