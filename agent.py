import os
from dotenv import load_dotenv
from caspian_sdk import CommClient
from openai import OpenAI

load_dotenv()

# The "brain" — Featherless gives it AI reasoning
ai = OpenAI(
    api_key=os.getenv("FEATHERLESS_API_KEY"),
    base_url="https://api.featherless.ai/v1"
)

# The "hands" — Caspian connects it to real channels
client = CommClient()

# Connect two channels — email and Discord
inbox = client.connect_email()
print("Email address:", inbox["address"])

discord = client.connect_discord(bot_token=os.getenv("DISCORD_BOT_TOKEN"))
print("Discord connected:", discord["status"])

# Simple memory — keeps conversation history per thread while the script runs
memory = {}

@client.on_message
def handle(message):
    print(f"[{message.channel}] {message.sender}: {message.text}")

    history = memory.get(message.conversation_id, [])
    history.append({"role": "user", "content": message.text})

    response = ai.chat.completions.create(
        model="deepseek-ai/DeepSeek-V3.2",
        messages=[
            {"role": "system", "content": "You are CampusPilot, a helpful AI assistant for university students. Be concise and friendly."}
        ] + history
    )

    answer = response.choices[0].message.content
    history.append({"role": "assistant", "content": answer})
    memory[message.conversation_id] = history

    message.reply(answer)

print("CampusPilot is live. Listening for messages...")
client.listen()