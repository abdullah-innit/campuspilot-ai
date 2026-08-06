import os
import json
from datetime import datetime
from dotenv import load_dotenv
from caspian_sdk import CommClient
from openai import OpenAI

load_dotenv()

MODEL = "deepseek-ai/DeepSeek-V3.2"
LAST_DISCORD_FILE = "last_discord.json"

ai = OpenAI(
    api_key=os.getenv("FEATHERLESS_API_KEY"),
    base_url="https://api.featherless.ai/v1"
)
client = CommClient()

inbox = client.connect_email()
print("Email address:", inbox["address"])

discord = client.connect_discord(bot_token=os.getenv("DISCORD_BOT_TOKEN"))
print("Discord connected:", discord["status"])

memory = {}
DEADLINES_FILE = "deadlines.json"
NOTES_FILE = "notes.json"

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def ai_call(messages, fallback="Sorry, I hit a technical issue processing that. Try again in a moment."):
    try:
        response = ai.chat.completions.create(model=MODEL, messages=messages)
        return response.choices[0].message.content
    except Exception as e:
        print("AI ERROR:", e)
        return fallback

def extract_deadline(text):
    prompt = f"""Extract assignment/deadline info from this announcement.
Respond ONLY with valid JSON, no other text, in this exact format:
{{"title": "short name", "due_date": "YYYY-MM-DD or null if not mentioned", "weight_percent": number or null, "course": "course name if mentioned or null", "urgent_change": true or false, "urgent_reason": "short reason or null"}}

Set urgent_change to true ONLY if this announces something disruptive: a cancellation, a deadline moved EARLIER, a rescheduled exam/quiz, or a surprise assignment. A normal new assignment with a future due date is NOT urgent.

Announcement:
{text}"""
    raw = ai_call([{"role": "user", "content": prompt}], fallback=None)
    if not raw:
        return None
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None

def priority_label(weight):
    if weight is None:
        return "Unknown weight — treat as medium priority until confirmed"
    if weight >= 15:
        return "HIGH priority — major grade impact"
    if weight >= 5:
        return "MEDIUM priority"
    return "LOW priority — small grade impact"

def find_course_notes(notes, course_query):
    course_query = course_query.strip().lower()
    for name in notes:
        if name.lower() == course_query:
            return name
    for name in notes:
        if course_query in name.lower() or name.lower() in course_query:
            return name
    return None

def generate_quiz(course_name, notes_text):
    prompt = f"""You are creating a practice quiz for a university student studying "{course_name}".
Based on the notes below, generate a quiz with:
- 3 multiple choice questions (4 options each, clearly mark the correct one)
- 2 short-answer questions

Format cleanly with numbers, and include an ANSWERS section at the end.

Notes:
{notes_text}
"""
    return ai_call([{"role": "user", "content": prompt}],
                    fallback="Sorry, I couldn't generate a quiz right now — try again shortly.")

@client.on_message
def handle(message):
    text = (message.text or "").strip()
    lower = text.lower()
    print(f"[{message.channel}] {message.sender}: {text[:80]}")

    # Remember your Discord conversation so we can proactively alert you later
    if message.channel == "discord":
        save_json(LAST_DISCORD_FILE, {"conversation_id": message.conversation_id})

    # --- Save notes ---
    if lower.startswith("notes:"):
        first_line, _, rest = text.partition("\n")
        course_name = first_line.split(":", 1)[1].strip()
        note_content = rest.strip()
        if not course_name or not note_content:
            message.reply("Format:\nNOTES: <Course Name>\n<paste your notes below>")
            return
        notes = load_json(NOTES_FILE, {})
        existing = notes.get(course_name, "")
        notes[course_name] = (existing + "\n\n" + note_content).strip()
        save_json(NOTES_FILE, notes)
        message.reply(f"Saved notes for '{course_name}'. Ask me to 'quiz me on {course_name}' anytime.")
        return

    # --- Quiz request ---
    if lower.startswith("quiz me on"):
        course_query = text[len("quiz me on"):].strip()
        notes = load_json(NOTES_FILE, {})
        if not notes:
            message.reply("No notes saved yet. Send:\nNOTES: <Course Name>\n<paste your notes>\nthen ask me to quiz you.")
            return
        matched = find_course_notes(notes, course_query)
        if not matched:
            available = ", ".join(notes.keys())
            message.reply(f"No notes for '{course_query}'. I have: {available}")
            return
        message.typing()
        quiz = generate_quiz(matched, notes[matched])
        message.reply(quiz)
        return

    # --- Email: deadline / urgent-change detection ---
    if message.channel == "email":
        info = extract_deadline(text)
        if info and info.get("title"):
            deadlines = load_json(DEADLINES_FILE, [])
            info["logged_at"] = datetime.now().isoformat()
            deadlines.append(info)
            save_json(DEADLINES_FILE, deadlines)
            label = priority_label(info.get("weight_percent"))
            reply = (
                f"Logged: {info['title']}\n"
                f"Course: {info.get('course') or 'not specified'}\n"
                f"Due: {info.get('due_date') or 'not specified'}\n"
                f"Weight: {info.get('weight_percent') or 'not specified'}%\n"
                f"Priority: {label}"
            )

            # Proactive cross-channel alert on urgent changes
            if info.get("urgent_change"):
                last_discord = load_json(LAST_DISCORD_FILE, {})
                convo_id = last_discord.get("conversation_id")
                if convo_id:
                    alert = (
                        f"URGENT UPDATE — {info.get('course') or 'a course'}\n"
                        f"{info.get('urgent_reason') or 'Something changed.'}\n"
                        f"Re: {info['title']} — now due {info.get('due_date') or 'unspecified'}"
                    )
                    try:
                        client.send_message(convo_id, text=alert)
                    except Exception as e:
                        print("PROACTIVE SEND ERROR:", e)
        else:
            reply = "Couldn't find deadline info in that email. To save notes instead, start with 'NOTES: <Course Name>'."
        message.reply(reply)
        return

    # --- Default: normal chat, deadline-aware ---
    history = memory.get(message.conversation_id, [])
    history.append({"role": "user", "content": text})

    deadlines = load_json(DEADLINES_FILE, [])
    deadline_context = json.dumps(deadlines) if deadlines else "No deadlines logged yet."

    answer = ai_call([
        {"role": "system", "content": (
            "You are CampusPilot, an AI assistant for university students. "
            "You DO have access to the student's logged deadlines below — this is real, current data. "
            "Never tell the student to check their portal themselves; you already have this info. "
            f"Deadlines: {deadline_context}"
        )}
    ] + history)

    history.append({"role": "assistant", "content": answer})
    memory[message.conversation_id] = history
    message.reply(answer)
print("CampusPilot is live. Listening for messages...")
client.listen()