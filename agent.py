import os
import json
import threading
import time
from datetime import datetime
from dotenv import load_dotenv
from caspian_sdk import CommClient
from openai import OpenAI

load_dotenv()

MODEL = "deepseek-ai/DeepSeek-V3.2"

ai = OpenAI(api_key=os.getenv("FEATHERLESS_API_KEY"), base_url="https://api.featherless.ai/v1")
client = CommClient()

inbox = client.connect_email()
print("Email address:", inbox["address"])
discord = client.connect_discord(bot_token=os.getenv("DISCORD_BOT_TOKEN"))
print("Discord connected:", discord["status"])

memory = {}
DEADLINES_FILE = "deadlines.json"
NOTES_FILE = "notes.json"
LAST_DISCORD_FILE = "last_discord.json"
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

def priority_label(weight):
    if weight is None:
        return "Unknown weight — treat as medium priority until confirmed"
    if weight >= 15:
        return "HIGH priority — major grade impact"
    if weight >= 5:
        return "MEDIUM priority"
    return "LOW priority — small grade impact"

def get_threshold(course):
    data = load_json(THRESHOLD_FILE, {})
    return data.get(course, data.get("_default", DEFAULT_THRESHOLD))

def attendance_percent(entry):
    total = entry["present"] + entry["absent"]
    return 100.0 if total == 0 else (entry["present"] / total) * 100

def attendance_buffer(entry, threshold):
    p, a = entry["present"], entry["absent"]
    t = threshold / 100
    return int((p / t) - p - a)

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

# ---------- TOOL IMPLEMENTATIONS ----------

def tool_log_deadline(title, due_date=None, weight_percent=None, course=None, urgent_change=False, urgent_reason=None):
    deadlines = load_json(DEADLINES_FILE, [])
    entry = {"title": title, "due_date": due_date, "weight_percent": weight_percent,
              "course": course, "logged_at": datetime.now().isoformat(), "reminders_sent": []}
    deadlines.append(entry)
    save_json(DEADLINES_FILE, deadlines)
    label = priority_label(weight_percent)
    result = (f"Logged: {title}\nCourse: {course or 'not specified'}\nDue: {due_date or 'not specified'}\n"
              f"Weight: {weight_percent if weight_percent is not None else 'not specified'}%\nPriority: {label}")
    if urgent_change:
        last_discord = load_json(LAST_DISCORD_FILE, {})
        convo_id = last_discord.get("conversation_id")
        if convo_id:
            alert = (f"URGENT UPDATE — {course or 'a course'}\n{urgent_reason or 'Something changed.'}\n"
                     f"Re: {title} — now due {due_date or 'unspecified'}")
            try:
                client.send_message(convo_id, text=alert)
            except Exception as e:
                print("PROACTIVE SEND ERROR:", e)
    return result

def tool_save_notes(course, notes_text):
    notes = load_json(NOTES_FILE, {})
    notes[course] = (notes.get(course, "") + "\n\n" + notes_text).strip()
    save_json(NOTES_FILE, notes)
    return f"Saved notes for '{course}'. Ask to be quizzed on it anytime."

def tool_generate_quiz(course):
    notes = load_json(NOTES_FILE, {})
    matched = next((n for n in notes if n.lower() == course.strip().lower()
                     or course.strip().lower() in n.lower() or n.lower() in course.strip().lower()), None)
    if not matched:
        available = ", ".join(notes.keys()) if notes else "none yet"
        return f"No notes found for '{course}'. Available: {available}"
    prompt = (f'Create a practice quiz for "{matched}" from these notes:\n{notes[matched]}\n'
              f"Include 3 MCQs (4 options, mark correct) and 2 short-answer questions, plus an ANSWERS section.")
    try:
        r = ai.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": prompt}])
        return r.choices[0].message.content
    except Exception as e:
        print("QUIZ GEN ERROR:", e)
        return "Sorry, couldn't generate the quiz right now."

def tool_log_attendance(course, status):
    if status not in ("present", "absent"):
        return "Status must be 'present' or 'absent'."
    data = load_json(ATTENDANCE_FILE, {})
    entry = data.setdefault(course, {"present": 0, "absent": 0})
    entry[status] += 1
    save_json(ATTENDANCE_FILE, data)
    threshold = get_threshold(course)
    pct = attendance_percent(entry)
    buffer = attendance_buffer(entry, threshold)
    result = f"Logged {status} for {course}. Current attendance: {pct:.1f}% (threshold: {threshold}%)."
    if pct < threshold:
        result += "\nBelow threshold — this could risk exam eligibility."
    elif buffer <= 2:
        result += f"\nHeads up — you can only miss {max(buffer, 0)} more before dropping below {threshold}%."
    return result

def tool_attendance_status():
    data = load_json(ATTENDANCE_FILE, {})
    if not data:
        return "No attendance logged yet."
    return "\n\n".join(attendance_report_line(c, e) for c, e in data.items())

def tool_set_threshold(value, course=None):
    data = load_json(THRESHOLD_FILE, {})
    data[course if course else "_default"] = value
    save_json(THRESHOLD_FILE, data)
    return f"Set attendance threshold for {course or 'default'} to {value}%."

TOOLS = [
    {"type": "function", "function": {"name": "log_deadline",
     "description": "Log a deadline/announcement extracted from a message. Use for anything resembling a course task with a date.",
     "parameters": {"type": "object", "properties": {
         "title": {"type": "string"}, "due_date": {"type": "string", "description": "YYYY-MM-DD"},
         "weight_percent": {"type": "number"}, "course": {"type": "string"},
         "urgent_change": {"type": "boolean", "description": "True for cancellations, moved-earlier dates, surprise tasks"},
         "urgent_reason": {"type": "string"}}, "required": ["title"]}}},
    {"type": "function", "function": {"name": "save_notes",
     "description": "Save lecture notes/past-paper text under a course for later quiz generation.",
     "parameters": {"type": "object", "properties": {"course": {"type": "string"}, "notes_text": {"type": "string"}},
                     "required": ["course", "notes_text"]}}},
    {"type": "function", "function": {"name": "generate_quiz",
     "description": "Generate a practice quiz from previously saved notes for a course.",
     "parameters": {"type": "object", "properties": {"course": {"type": "string"}}, "required": ["course"]}}},
    {"type": "function", "function": {"name": "log_attendance",
     "description": "Log present/absent attendance for a course.",
     "parameters": {"type": "object", "properties": {"course": {"type": "string"},
         "status": {"type": "string", "enum": ["present", "absent"]}}, "required": ["course", "status"]}}},
    {"type": "function", "function": {"name": "attendance_status",
     "description": "Get attendance summary across all logged courses.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "set_attendance_threshold",
     "description": "Set minimum attendance % policy, globally or per course.",
     "parameters": {"type": "object", "properties": {"value": {"type": "number"}, "course": {"type": "string"}},
                     "required": ["value"]}}},
]

TOOL_IMPL = {"log_deadline": tool_log_deadline, "save_notes": tool_save_notes,
             "generate_quiz": tool_generate_quiz, "log_attendance": tool_log_attendance,
             "attendance_status": tool_attendance_status, "set_attendance_threshold": tool_set_threshold}

def build_system_prompt():
    deadlines = load_json(DEADLINES_FILE, [])
    summary = json.dumps(deadlines) if deadlines else "No deadlines logged yet."
    return (
        "You are CampusPilot, an autonomous university assistant. You have real tools — use them, don't just describe what you'd do.\n"
        "- Course announcement or deadline mentioned → call log_deadline.\n"
        "- Pasted lecture notes/past-paper text → call save_notes.\n"
        "- Asked to be quizzed → call generate_quiz.\n"
        "- Reports attending/missing class → call log_attendance.\n"
        "- Asks about attendance → call attendance_status.\n"
        "- Wants to set attendance policy → call set_attendance_threshold.\n"
        "- Anything else → reply normally, no tool call, but stay aware of current deadlines below.\n"
        "Never say you lack access to something a tool could get you.\n"
        f"Current logged deadlines (already known): {summary}"
    )

def run_agent(history):
    messages = [{"role": "system", "content": build_system_prompt()}] + history
    try:
        response = ai.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS, tool_choice="auto")
    except Exception as e:
        print("AI ERROR:", e)
        return "Sorry, I hit a technical issue processing that. Try again in a moment."

    msg = response.choices[0].message
    if msg.tool_calls:
        messages.append(msg)
        for call in msg.tool_calls:
            try:
                args = json.loads(call.function.arguments)
            except json.JSONDecodeError:
                args = {}
            fn = TOOL_IMPL.get(call.function.name)
            result = fn(**args) if fn else f"Unknown tool: {call.function.name}"
            messages.append({"role": "tool", "tool_call_id": call.id, "content": str(result)})
        try:
            followup = ai.chat.completions.create(model=MODEL, messages=messages)
            return followup.choices[0].message.content
        except Exception as e:
            print("AI FOLLOWUP ERROR:", e)
            return "Done, but couldn't summarize the result — check the terminal logs."
    return msg.content

REMINDER_TIERS = [(3, "3-day"), (1, "1-day"), (0, "due-today")]

def check_reminders():
    while True:
        try:
            deadlines = load_json(DEADLINES_FILE, [])
            convo_id = load_json(LAST_DISCORD_FILE, {}).get("conversation_id")
            updated = False
            if convo_id:
                today = datetime.now().date()
                for d in deadlines:
                    if not d.get("due_date"):
                        continue
                    try:
                        due_date = datetime.strptime(d["due_date"], "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    days_left = (due_date - today).days
                    sent = d.setdefault("reminders_sent", [])
                    for threshold, tag in REMINDER_TIERS:
                        if days_left == threshold and tag not in sent:
                            when = "today" if threshold == 0 else f"in {threshold} day(s)"
                            text = (f"Reminder: {d['title']} ({d.get('course') or 'course not specified'}) "
                                    f"is due {when} — worth {d.get('weight_percent') or 'unspecified'}%.")
                            try:
                                client.send_message(convo_id, text=text)
                                sent.append(tag)
                                updated = True
                            except Exception as e:
                                print("REMINDER SEND ERROR:", e)
            if updated:
                save_json(DEADLINES_FILE, deadlines)
        except Exception as e:
            print("REMINDER LOOP ERROR:", e)
        time.sleep(1800)

@client.on_message
def handle(message):
    text = (message.text or "").strip()
    print(f"[{message.channel}] {message.sender}: {text[:80]}")

    if message.channel == "discord":
        save_json(LAST_DISCORD_FILE, {"conversation_id": message.conversation_id})

    message.typing()
    history = memory.get(message.conversation_id, [])
    history.append({"role": "user", "content": text})

    answer = run_agent(history)

    history.append({"role": "assistant", "content": answer})
    memory[message.conversation_id] = history
    message.reply(answer)

threading.Thread(target=check_reminders, daemon=True).start()
print("CampusPilot is live. Listening for messages...")
client.listen()