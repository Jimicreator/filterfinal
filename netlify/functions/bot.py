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
# IMPORT THE BRIDGE
from serverless_wsgi import handle_request

# --- CONFIGURATION ---
TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URL = os.environ.get("MONGO_URL")

# Safe Integer Conversion
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
    VAULT_CHANNEL_ID = int(os.environ.get("CHANNEL_ID", 0))
except:
    ADMIN_ID = 0
    VAULT_CHANNEL_ID = 0

# --- DATABASE ---
client = MongoClient(MONGO_URL)
db = client["adalat_library"]

courses_col = db["courses"]
users_col = db["users"]
settings_col = db["settings"]
logs_col = db["logs"]

# --- FLASK APP ---
app = Flask(__name__)

# --- HELPERS ---
def log_event(event_type, user_id=None, extra=None):
    try:
        entry = {
            "event": event_type,
            "user_id": user_id,
            "extra": extra or {},
            "time": datetime.utcnow()
        }
        logs_col.insert_one(entry)
        print(f"[{event_type}] user={user_id}")
    except Exception as e:
        print(f"Log Error: {e}")

def get_clean_caption(text, filename):
    if not text: text = filename
    lines = text.split("\n")
    clean = []
    for line in lines:
        if "@" in line or "join" in line.lower() or "t.me" in line: continue
        clean.append(line)
    result = "\n".join(clean).strip()
    return f"{result or filename}\n\nDownloaded via @Adalat_One_Bot ⚖️"

# --- ACCESS CONTROL ---
async def check_access(user_id):
    config = settings_col.find_one({"_id": "config"})
    if not config or not config.get("lock_channels"): return True, None
    
    lock_channel_id = config["lock_channels"][0]
    user = users_col.find_one({"user_id": user_id})
    
    # Simple check: If they requested join, we assume they are pending or approved
    if user and user.get("requested_join"):
        return True, None
        
    return False, lock_channel_id

# --- BOT LOGIC ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    is_allowed, lock_id = await check_access(user_id)
    if not is_allowed:
        await update.message.reply_text("🔒 You must request to join our private channel first.")
        return

    if args:
        token = args[0]
        course = courses_col.find_one({"files.token": token})
        if course:
            target_file = next((f for f in course["files"] if f["token"] == token), None)
            if target_file:
                await context.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=VAULT_CHANNEL_ID,
                    message_id=target_file["msg_id"],
                    caption=target_file["caption"]
                )
                log_event("file_delivered", user_id, {"file": target_file["name"]})
                return
        await update.message.reply_text("❌ File not found.")
        return

    await update.message.reply_text("⚖️ **Adalat Library**\n\nType a course name to search.")

async def new_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    name = " ".join(context.args)
    if not name: return await update.message.reply_text("Usage: /newcourse Name")
    
    res = courses_col.insert_one({"title": name, "status": "uploading", "files": []})
    settings_col.update_one({"_id": "admin_state"}, {"$set": {"mode": "uploading", "course_id": res.inserted_id}}, upsert=True)
    await update.message.reply_text(f"📂 Opened '{name}'. Forward files to Vault.")

async def finish_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    settings_col.update_one({"_id": "admin_state"}, {"$set": {"mode": "idle"}})
    courses_col.update_many({"status": "uploading"}, {"$set": {"status": "live"}})
    await update.message.reply_text("✅ Course is Live.")

async def add_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        cid = int(context.args[0])
        settings_col.update_one({"_id": "config"}, {"$addToSet": {"lock_channels": cid}}, upsert=True)
        await update.message.reply_text(f"🔒 Added {cid}")
    except:
        await update.message.reply_text("Usage: /addlock -100xxxxxxxx")

async def channel_post_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.channel_post.chat.id != VAULT_CHANNEL_ID: return
    state = settings_col.find_one({"_id": "admin_state"})
    if not state or state.get("mode") != "uploading": return

    msg = update.channel_post
    if msg.video or msg.document:
        random_token = str(uuid.uuid4())
        file_name = msg.caption.split("\n")[0] if msg.caption else "File"
        
        courses_col.update_one(
            {"_id": state["course_id"]}, 
            {"$push": {"files": {
                "token": random_token,
                "msg_id": msg.message_id,
                "name": file_name[:50],
                "caption": get_clean_caption(msg.caption, file_name)
            }}}
        )
        log_event("indexed", ADMIN_ID, {"file": file_name})

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = update.message.text
    
    is_allowed, _ = await check_access(user_id)
    if not is_allowed: return await update.message.reply_text("🔒 Join channel first.")

    results = list(courses_col.find({"title": {"$regex": query, "$options": "i"}, "status": "live"}).limit(5))
    if not results: return await update.message.reply_text("❌ No courses found.")

    buttons = [[InlineKeyboardButton(f"📚 {c['title']}", callback_data=f"view|{c['_id']}")] for c in results]
    await update.message.reply_text(f"Found {len(results)} courses:", reply_markup=InlineKeyboardMarkup(buttons))

async def menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, course_id = update.callback_query.data.split("|")
    from bson.objectid import ObjectId
    course = courses_col.find_one({"_id": ObjectId(course_id)})
    
    msg = f"💿 **{course['title']}**\n\n"
    for f in course["files"]:
        link = f"https://t.me/{context.bot.username}?start={f['token']}"
        msg += f"📄 [{f['name']}]({link})\n"
    await update.callback_query.message.reply_text(msg, parse_mode="Markdown")

async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.chat_join_request.from_user.id
    users_col.update_one({"user_id": user_id}, {"$set": {"requested_join": True}}, upsert=True)
    try: await context.bot.send_message(user_id, "📝 Request received! You can now search.")
    except: pass

# --- APP BUILDER ---
ptb_app = Application.builder().token(TOKEN).build()
ptb_app.add_handler(ChatJoinRequestHandler(join_request))
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("newcourse", new_course))
ptb_app.add_handler(CommandHandler("finish", finish_upload))
ptb_app.add_handler(CommandHandler("addlock", add_lock))
ptb_app.add_handler(MessageHandler(filters.Chat(VAULT_CHANNEL_ID), channel_post_listener))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search))
ptb_app.add_handler(CallbackQueryHandler(menu_click))

# --- NETLIFY HANDLER (THE MISSING PIECE) ---
@app.route("/", methods=["POST"])
def webhook():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), ptb_app.bot)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(ptb_app.process_update(update))
        loop.close()
    return "OK"

def handler(event, context):
    return handle_request(app, event, context)