import os
import uuid
import asyncio
from datetime import datetime

from flask import Flask, request
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ChatJoinRequestHandler,
    filters, ContextTypes
)

# --- CONFIG ---
TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URL = os.environ.get("MONGO_URL")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
VAULT_CHANNEL_ID = int(os.environ.get("CHANNEL_ID"))

# --- DB ---
client = MongoClient(MONGO_URL)
db = client["adalat_library"]
courses_col = db["courses"]
users_col = db["users"]
settings_col = db["settings"]
logs_col = db["logs"]

# --- FLASK APP ---
app = Flask(__name__)

# --- PTB APP ---
ptb_app = Application.builder().token(TOKEN).build()


# -------- LOGGING --------
def log_event(event_type, user_id=None, extra=None):
    data = {
        "event": event_type,
        "user_id": user_id,
        "extra": extra or {},
        "time": datetime.utcnow(),
    }
    logs_col.insert_one(data)
    print(f"[{event_type}] {user_id} :: {extra}")


# -------- HELPERS --------
def get_clean_caption(text, filename):
    if not text:
        text = filename
    lines = text.split("\n")
    clean = []
    for line in lines:
        if "@" in line or "join" in line.lower() or "t.me" in line:
            continue
        clean.append(line)
    result = "\n".join(clean).strip() or filename
    return f"{result}\n\nDownloaded via @Adalat_One_Bot ⚖️"


async def check_access(user_id):
    """User must: (1) have requested join, (2) still be member of lock channel"""
    config = settings_col.find_one({"_id": "config"})
    if not config or not config.get("lock_channels"):
        return True, None

    lock_channel_id = config["lock_channels"][0]

    user = users_col.find_one({"user_id": user_id})
    if not user or not user.get("requested_join"):
        log_event("blocked_no_join_request", user_id)
        return False, lock_channel_id

    try:
        member = await ptb_app.bot.get_chat_member(lock_channel_id, user_id)
        if member.status in ["member", "administrator", "creator"]:
            return True, None
        log_event("blocked_not_member", user_id)
        return False, lock_channel_id
    except Exception as e:
        log_event("access_check_error", user_id, {"error": str(e)})
        return False, lock_channel_id


# -------- HANDLERS --------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    allowed, lock_id = await check_access(user_id)
    if not allowed:
        await update.message.reply_text(
            "🔒 Access denied.\nRequest to join our channel first."
        )
        return

    # token deep-link delivery
    if args:
            token = args[0]
            log_event("file_request", user_id, {"token": token})

            course = courses_col.find_one({"files.token": token})
            if not course:
                log_event("invalid_token", user_id, {"token": token})
                await update.message.reply_text("❌ File token expired or invalid.")
                return

            file = next((f for f in course["files"] if f["token"] == token), None)
            if not file:
                await update.message.reply_text("❌ File token expired or invalid.")
                return

            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=VAULT_CHANNEL_ID,
                message_id=file["msg_id"],
                caption=file["caption"],
            )

            log_event("file_delivered", user_id, {"file": file["name"]})
            return

    await update.message.reply_text(
        "⚖️ **Adalat Library**\n\nType a course name to search.\nExample: React, Python"
    )


async def new_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    name = " ".join(context.args)
    res = courses_col.insert_one({"title": name, "status": "uploading", "files": []})

    settings_col.update_one(
        {"_id": "admin_state"},
        {"$set": {"mode": "uploading", "course_id": res.inserted_id}},
        upsert=True,
    )

    log_event("new_course", ADMIN_ID, {"title": name})
    await update.message.reply_text(
        f"📂 Opened '{name}'. Forward files to the vault channel."
    )


async def channel_post_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.channel_post.chat.id != VAULT_CHANNEL_ID:
        return

    state = settings_col.find_one({"_id": "admin_state"})
    if not state or state.get("mode") != "uploading":
        return

    msg = update.channel_post
    if msg.video or msg.document:
        token = str(uuid.uuid4())
        filename = msg.caption.split("\n")[0] if msg.caption else "File"
        caption = get_clean_caption(msg.caption, filename)

        file_data = {
            "token": token,
            "msg_id": msg.message_id,
            "name": filename[:50],
            "caption": caption,
        }

        courses_col.update_one(
            {"_id": state["course_id"]}, {"$push": {"files": file_data}}
        )

        log_event("file_indexed", ADMIN_ID, {"name": filename})
        print("Indexed:", filename)


async def finish_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    settings_col.update_one({"_id": "admin_state"}, {"$set": {"mode": "idle"}})
    courses_col.update_many({"status": "uploading"}, {"$set": {"status": "live"}})

    log_event("finish_upload", ADMIN_ID)
    await update.message.reply_text("✅ Course is Live.")


async def add_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    try:
        channel_id = int(context.args[0])
        settings_col.update_one(
            {"_id": "config"},
            {"$addToSet": {"lock_channels": channel_id}},
            upsert=True,
        )
        log_event("lock_added", ADMIN_ID, {"channel": channel_id})
        await update.message.reply_text(f"🔒 Added {channel_id} to lock list.")
    except:
        await update.message.reply_text("Usage: /addlock -100xxxxxxxx")


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = update.message.text

    log_event("search", user_id, {"query": query})

    allowed, _ = await check_access(user_id)
    if not allowed:
        await update.message.reply_text("🔒 Join channel first.")
        return

    results = list(
        courses_col.find(
            {"title": {"$regex": query, "$options": "i"}, "status": "live"}
        ).limit(5)
    )

    if not results:
        await update.message.reply_text("❌ No courses found.")
        return

    buttons = [
        [InlineKeyboardButton(f"📚 {c['title']}", callback_data=f"view|{c['_id']}")]
        for c in results
    ]

    await update.message.reply_text(
        f"Found {len(results)} courses:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


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


async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.chat_join_request.from_user.id

    users_col.update_one(
        {"user_id": user_id},
        {"$set": {"requested_join": True}},
        upsert=True,
    )

    log_event("join_request_detected", user_id)

    try:
        await context.bot.send_message(
            user_id, "📝 Join request received. Wait for manual approval."
        )
    except:
        pass


# --- Register handlers ---
ptb_app.add_handler(ChatJoinRequestHandler(join_request))
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("newcourse", new_course))
ptb_app.add_handler(CommandHandler("finish", finish_upload))
ptb_app.add_handler(CommandHandler("addlock", add_lock))
ptb_app.add_handler(MessageHandler(filters.Chat(VAULT_CHANNEL_ID), channel_post_listener))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search))
ptb_app.add_handler(CallbackQueryHandler(menu_click))


# -------- WEBHOOK ENDPOINTS --------
@app.route("/", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), ptb_app.bot)
    asyncio.run(ptb_app.process_update(update))
    return "OK", 200


@app.route("/health")
def health():
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
