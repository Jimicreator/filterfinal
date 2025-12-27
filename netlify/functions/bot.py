import os
import uuid
from datetime import datetime

from flask import Flask, request
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ChatJoinRequestHandler,
    filters, ContextTypes
)

# --- CONFIGURATION ---
TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URL = os.environ.get("MONGO_URL")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
VAULT_CHANNEL_ID = int(os.environ.get("CHANNEL_ID"))

# --- DATABASE ---
client = MongoClient(MONGO_URL)
db = client["adalat_library"]

courses_col = db["courses"]
users_col = db["users"]
settings_col = db["settings"]
logs_col = db["logs"]   # <-- NEW LOG COLLECTION

# --- FLASK APP ---
app = Flask(__name__)

# --- LOGGING SYSTEM ---

def log_event(event_type, user_id=None, extra=None):
    entry = {
        "event": event_type,
        "user_id": user_id,
        "extra": extra or {},
        "time": datetime.utcnow()
    }
    logs_col.insert_one(entry)
    print(f"[{event_type}] user={user_id} :: {extra}")

# --- HELPERS ---

def get_clean_caption(text, filename):
    if not text:
        text = filename
    lines = text.split("\n")
    clean = []
    for line in lines:
        if "@" in line or "join" in line.lower() or "t.me" in line:
            continue
        clean.append(line)

    result = "\n".join(clean).strip()
    if not result:
        result = filename
    return f"{result}\n\nDownloaded via @Adalat_One_Bot ⚖️"


# --- ACCESS CONTROL (STRICT MODE) ---

async def check_access(user_id):
    """
    Rules:
    1) User must have sent join request
    2) User must currently be a channel member
    """

    config = settings_col.find_one({"_id": "config"})
    if not config or not config.get("lock_channels"):
        return True, None

    lock_channel_id = config["lock_channels"][0]

    # user must have SENT a join request earlier
    user = users_col.find_one({"user_id": user_id})
    if not user or not user.get("requested_join"):
        log_event("access_blocked_no_join_request", user_id)
        return False, lock_channel_id

    # verify REAL membership before granting access
    try:
        member = await ptb_app.bot.get_chat_member(lock_channel_id, user_id)

        if member.status in ["member", "administrator", "creator"]:
            return True, None

        log_event("access_blocked_not_member", user_id)
        return False, lock_channel_id

    except Exception as e:
        log_event("access_check_error", user_id, {"error": str(e)})
        return False, lock_channel_id


# --- BOT LOGIC ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    is_allowed, lock_channel_id = await check_access(user_id)
    if not is_allowed:
        await update.message.reply_text(
            "🔒 You must request to join our private channel first."
        )
        return

    # deep-link file access via token
    if args:
        token = args[0]
        log_event("file_request", user_id, {"token": token})

        course = courses_col.find_one({"files.token": token})
        if not course:
            log_event("invalid_token", user_id, {"token": token})
            await update.message.reply_text("❌ File token expired or invalid.")
            return

        target_file = next((f for f in course["files"] if f["token"] == token), None)
        if not target_file:
            log_event("missing_token_entry", user_id, {"token": token})
            await update.message.reply_text("❌ File token expired or invalid.")
            return

        await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=VAULT_CHANNEL_ID,
            message_id=target_file["msg_id"],
            caption=target_file["caption"]
        )

        log_event("file_delivered", user_id, {"file": target_file["name"]})
        return

    await update.message.reply_text(
        "⚖️ **Adalat Library**\n\nType a course name to search.\nExample: React, Python"
    )


# --- ADMIN: NEW COURSE ---

async def new_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    name = " ".join(context.args)
    res = courses_col.insert_one({"title": name, "status": "uploading", "files": []})

    settings_col.update_one(
        {"_id": "admin_state"},
        {"$set": {"mode": "uploading", "course_id": res.inserted_id}},
        upsert=True
    )

    log_event("admin_new_course", ADMIN_ID, {"title": name})
    await update.message.reply_text(f"📂 Opened '{name}'. Forward files to Vault.")


# --- AUTO INDEXER (UPLOAD MODE) ---

async def channel_post_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.channel_post.chat.id != VAULT_CHANNEL_ID:
        return

    state = settings_col.find_one({"_id": "admin_state"})
    if not state or state.get("mode") != "uploading":
        return

    msg = update.channel_post

    if msg.video or msg.document:
        random_token = str(uuid.uuid4())
        file_name = msg.caption.split("\n")[0] if msg.caption else "File"
        clean_cap = get_clean_caption(msg.caption, file_name)

        file_entry = {
            "token": random_token,
            "msg_id": msg.message_id,
            "name": file_name[:50],
            "caption": clean_cap
        }

        courses_col.update_one(
            {"_id": state["course_id"]},
            {"$push": {"files": file_entry}}
        )

        log_event("file_indexed", ADMIN_ID, {"name": file_name})
        print(f"Indexed: {file_name}")


# --- FINISH UPLOAD ---

async def finish_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    settings_col.update_one({"_id": "admin_state"}, {"$set": {"mode": "idle"}})
    courses_col.update_many({"status": "uploading"}, {"$set": {"status": "live"}})

    log_event("admin_finish_upload", ADMIN_ID)
    await update.message.reply_text("✅ Course is Live.")


# --- ADD LOCK CHANNEL ---

async def add_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    try:
        channel_id = int(context.args[0])

        settings_col.update_one(
            {"_id": "config"},
            {"$addToSet": {"lock_channels": channel_id}},
            upsert=True
        )

        log_event("lock_channel_added", ADMIN_ID, {"channel": channel_id})
        await update.message.reply_text(f"🔒 Added {channel_id} to lock list.")

    except:
        await update.message.reply_text("Usage: /addlock -100xxxxxxxx")


# --- SEARCH ---

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = update.message.text

    log_event("search", user_id, {"query": query})

    is_allowed, _ = await check_access(user_id)
    if not is_allowed:
        await update.message.reply_text("🔒 Join channel first.")
        return

    results = list(
        courses_col.find(
            {"title": {"$regex": query, "$options": "i"}, "status": "live"}
        ).limit(5)
    )

    if not results:
        log_event("search_no_results", user_id, {"query": query})
        await update.message.reply_text("❌ No courses found.")
        return

    buttons = [
        [InlineKeyboardButton(f"📚 {c['title']}", callback_data=f"view|{c['_id']}")]
        for c in results
    ]

    await update.message.reply_text(
        f"Found {len(results)} courses:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# --- MENU CLICK ---

async def menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, course_id = query.data.split("|")

    from bson.objectid import ObjectId
    course = courses_col.find_one({"_id": ObjectId(course_id)})

    msg = f"💿 **{course['title']}**\n\n"

    for f in course["files"]:
        link = f"https://t.me/{context.bot.username}?start={f['token']}"
        msg += f"📄 [{f['name']}]({link})\n"

    await query.message.reply_text(msg, parse_mode="Markdown")


# --- JOIN REQUEST (DO NOT AUTO-APPROVE) ---

async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.chat_join_request.from_user.id

    users_col.update_one(
        {"user_id": user_id},
        {"$set": {"requested_join": True}},
        upsert=True
    )

    log_event("join_request_detected", user_id)

    try:
        await context.bot.send_message(
            user_id,
            "📝 Join request received. Wait for manual approval."
        )
    except:
        pass


# --- ADMIN: VIEW LOGS ---

async def show_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    last = logs_col.find().sort("time", -1).limit(10)

    msg = "📜 **Recent Logs**\n\n"
    for l in last:
        msg += f"{l['time']} — {l['event']} — {l.get('user_id')} — {l.get('extra')}\n"

    await update.message.reply_text(msg)


# --- APP BUILDER ---

ptb_app = Application.builder().token(TOKEN).build()

ptb_app.add_handler(ChatJoinRequestHandler(join_request))
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("newcourse", new_course))
ptb_app.add_handler(CommandHandler("finish", finish_upload))
ptb_app.add_handler(CommandHandler("addlock", add_lock))
ptb_app.add_handler(CommandHandler("logs", show_logs))
ptb_app.add_handler(MessageHandler(filters.Chat(VAULT_CHANNEL_ID), channel_post_listener))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search))
ptb_app.add_handler(CallbackQueryHandler(menu_click))


# --- WEBHOOK ---

@app.route("/", methods=["POST"])
def webhook():
    import asyncio
    update = Update.de_json(request.get_json(force=True), ptb_app.bot)
    asyncio.run(ptb_app.process_update(update))
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
