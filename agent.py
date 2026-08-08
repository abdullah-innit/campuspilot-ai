import threading
import time
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
ATTENDANCE_FILE = "attendance.json"
THRESHOLD_FILE = "attendance_threshold.json"
DEFAULT_THRESHOLD = 75

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def get_threshold(course):
    data = load_json(THRESHOLD_FILE, {})
    return data.get(course, data.get("_default", DEFAULT_THRESHOLD))

def set_threshold(value, course=None):
    data = load_json(THRESHOLD_FILE, {})
    key = course if course else "_default"
    data[key] = value
    save_json(THRESHOLD_FILE, data)

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
        parsed = json.loads(raw)
        title = parsed.get("title")
        if not title or str(title).strip().lower() in ("null", "none", "n/a", ""):
            return None
        return parsed
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

REMINDER_TIERS = [(3, "3-day"), (1, "1-day"), (0, "due-today")]

def check_reminders():
    while True:
        try:
            deadlines = load_json(DEADLINES_FILE, [])
            last_discord = load_json(LAST_DISCORD_FILE, {})
            convo_id = last_discord.get("conversation_id")
            updated = False

            if convo_id:
                today = datetime.now().date()
                for d in deadlines:
                    due_str = d.get("due_date")
                    if not due_str:
                        continue
                    try:
                        due_date = datetime.strptime(due_str, "%Y-%m-%d").date()
                    except ValueError:
                        continue

                    days_left = (due_date - today).days
                    sent = d.setdefault("reminders_sent", [])

                    for threshold, tag in REMINDER_TIERS:
                        if days_left == threshold and tag not in sent:
                            when = "today" if threshold == 0 else f"in {threshold} day(s)"
                            reminder_text = (
                                f"Reminder: {d['title']} "
                                f"({d.get('course') or 'course not specified'}) "
                                f"is due {when} — worth {d.get('weight_percent') or 'unspecified'}%."
                            )
                            try:
                                client.send_message(convo_id, text=reminder_text)
                                sent.append(tag)
                                updated = True
                            except Exception as e:
                                print("REMINDER SEND ERROR:", e)

            if updated:
                save_json(DEADLINES_FILE, deadlines)
        except Exception as e:
            print("REMINDER LOOP ERROR:", e)

        time.sleep(1800)  # check every 30 minutes

def record_attendance(course, status):
    data = load_json(ATTENDANCE_FILE, {})
    entry = data.setdefault(course, {"present": 0, "absent": 0})
    entry["present" if status == "present" else "absent"] += 1
    save_json(ATTENDANCE_FILE, data)
    return entry

def attendance_percent(entry):
    total = entry["present"] + entry["absent"]
    return 100.0 if total == 0 else (entry["present"] / total) * 100

def attendance_buffer(entry, threshold):
    p, a = entry["present"], entry["absent"]
    t = threshold / 100
    max_total_absences = (p / t) - p
    return int(max_total_absences - a)

def attendance_report_line(course, entry):
    threshold = get_threshold(course)
    pct = attendance_percent(entry)
    buffer = attendance_buffer(entry, threshold)
    status = "OK" if pct >= threshold else "AT RISK"
    line = f"{course}: {pct:.1f}% ({entry['present']} present / {entry['absent']} absent) — {status} (threshold: {threshold}%)"
    if pct >= threshold:
        line += f"\n   Can miss {max(buffer, 0)} more before dropping below {threshold}%."
    else:
        line += f"\n   Below the {threshold}% threshold — attend consistently to recover."
    return line

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

# --- Set attendance threshold: "set attendance threshold: 80" or "set attendance threshold: OOP 80" ---
    if lower.startswith("set attendance threshold"):
        rest = text.split(":", 1)[-1].strip()
        parts = rest.rsplit(" ", 1)
        try:
            if len(parts) == 2 and parts[1].replace(".", "").isdigit():
                course, value = parts[0].strip(), float(parts[1])
                set_threshold(value, course)
                message.reply(f"Set attendance threshold for {course} to {value}%.")
            elif rest.replace(".", "").isdigit():
                set_threshold(float(rest))
                message.reply(f"Set default attendance threshold to {rest}%.")
            else:
                raise ValueError
        except ValueError:
            message.reply("Format:\nset attendance threshold: 80\nor per-course:\nset attendance threshold: OOP 80")
        return

    # --- Attendance logging ---
    if lower.startswith("attendance:"):
        rest = text.split(":", 1)[1].strip()
        parts = rest.rsplit(" ", 1)
        if len(parts) != 2 or parts[1].lower() not in ("present", "absent"):
            message.reply("Format:\nattendance: <Course Name> present\nor\nattendance: <Course Name> absent")
            return
        course, status = parts[0].strip(), parts[1].lower()
        entry = record_attendance(course, status)
        threshold = get_threshold(course)
        pct = attendance_percent(entry)
        buffer = attendance_buffer(entry, threshold)
        reply = f"Logged {status} for {course}. Current attendance: {pct:.1f}% (threshold: {threshold}%)."
        if pct < threshold:
            reply += "\nBelow threshold — this could risk exam eligibility."
        elif buffer <= 2:
            reply += f"\nHeads up — you can only miss {max(buffer, 0)} more before dropping below {threshold}%."
        message.reply(reply)
        return

    # --- Attendance summary: "attendance status" / "my attendance" ---
    if lower in ("attendance status", "my attendance", "attendance"):
        data = load_json(ATTENDANCE_FILE, {})
        if not data:
            message.reply("No attendance logged yet. Use: attendance: <Course Name> present/absent")
            return
        lines = [attendance_report_line(c, e) for c, e in data.items()]
        message.reply("Attendance summary:\n\n" + "\n\n".join(lines))
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
threading.Thread(target=check_reminders, daemon=True).start()    
print("CampusPilot is live. Listening for messages...")
client.listen()