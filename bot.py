"""
📰 Briefing Diário — Bot Telegram
RSS feeds → Claude (sem web search) → Telegram
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime

import re

import anthropic
import feedparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update, Bot
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ── LOGGING ───────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL   = os.environ["TELEGRAM_CHANNEL_ID"]
ADMIN_TELEGRAM_ID  = os.environ.get("TELEGRAM_ADMIN_ID", "")

MODEL       = "claude-haiku-4-5-20251001"
MAX_HISTORY = 12

conversation_history: dict[int, list[dict]] = {}
last_briefing: dict | None = None

# ── RSS FEEDS ─────────────────────────────────────────────────
FEEDS = {
    "portugal": {
        "name": "Portugal & Política", "emoji": "🇵🇹",
        "urls": [
            "https://www.publico.pt/rss",
            "https://observador.pt/feed/",
            "https://expresso.pt/rss",
        ]
    },
    "business": {
        "name": "Economia & Business", "emoji": "💼",
        "urls": [
            "https://feeds.bbci.co.uk/news/business/rss.xml",
            "https://www.jornaldenegocios.pt/rss",
            "https://feeds.reuters.com/reuters/businessNews",
        ]
    },
    "mundo": {
        "name": "Mundo & Geopolítica", "emoji": "🌍",
        "urls": [
            "https://feeds.bbci.co.uk/news/world/rss.xml",
            "https://feeds.reuters.com/reuters/worldNews",
            "https://www.politico.eu/feed/",
        ]
    },
    "saude": {
        "name": "Saúde & Ciência", "emoji": "🧬",
        "urls": [
            "https://feeds.bbci.co.uk/news/health/rss.xml",
            "https://feeds.feedburner.com/stat-news",
            "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        ]
    },
    "tech": {
        "name": "Tech & IA", "emoji": "🤖",
        "urls": [
            "https://feeds.arstechnica.com/arstechnica/index",
            "https://www.wired.com/feed/rss",
            "https://feeds.feedburner.com/TechCrunch",
        ]
    },
    "desporto": {
        "name": "Desporto", "emoji": "⚽",
        "urls": [
            "https://feeds.bbci.co.uk/sport/rss.xml",
            "https://www.abola.pt/rss/index.aspx",
            "https://www.record.pt/rss",
        ]
    },
}

# ── DATE ─────────────────────────────────────────────────────
WEEKDAYS = ["segunda-feira","terça-feira","quarta-feira","quinta-feira","sexta-feira","sábado","domingo"]
MONTHS   = ["","janeiro","fevereiro","março","abril","maio","junho","julho","agosto","setembro","outubro","novembro","dezembro"]

def today_label() -> str:
    n = datetime.now()
    return f"{WEEKDAYS[n.weekday()]}, {n.day} de {MONTHS[n.month]} de {n.year}"

# ── HELPERS ───────────────────────────────────────────────────
def extract_json(text: str) -> dict | None:
    s = text.strip().replace("```json","").replace("```","").strip()
    try: return json.loads(s)
    except Exception: pass
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b > a:
        try: return json.loads(s[a:b+1])
        except Exception: pass
    return None

def is_admin(user_id: int) -> bool:
    return not ADMIN_TELEGRAM_ID or str(user_id) == ADMIN_TELEGRAM_ID.strip()

def split_message(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit: return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current: parts.append(current.strip())
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip(): parts.append(current.strip())
    return parts

def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()

# ── RSS FETCHING ──────────────────────────────────────────────
def fetch_category(cat_id: str, max_per_feed: int = 4) -> list[dict]:
    cat   = FEEDS[cat_id]
    items = []
    for url in cat["urls"]:
        try:
            feed = feedparser.parse(url)
            source = feed.feed.get("title", url)
            for entry in feed.entries[:max_per_feed]:
                title   = strip_html(entry.get("title", "")).strip()
                summary = strip_html(entry.get("summary", entry.get("description", ""))).strip()
                link    = entry.get("link", "")
                if title:
                    items.append({"title": title, "summary": summary[:250], "link": link, "source": source})
            log.info(f"  RSS {url}: {len(feed.entries)} entries")
        except Exception as e:
            log.warning(f"  RSS failed {url}: {e}")
    return items[:10]

def build_rss_context(today: str) -> str:
    lines = [f"Today is {today}. Latest headlines from RSS feeds:\n"]
    for cat_id, cat in FEEDS.items():
        items = fetch_category(cat_id)
        lines.append(f"=== {cat['name']} ===")
        if items:
            for item in items:
                lines.append(f"• {item['title']}")
                if item["summary"]:
                    lines.append(f"  {item['summary'][:150]}")
                lines.append(f"  URL: {item['link']}  Source: {item['source']}")
        else:
            lines.append("  (no items fetched)")
        lines.append("")
    return "\n".join(lines)

# ── BRIEFING GENERATION ───────────────────────────────────────
def generate_briefing(today: str) -> dict:
    log.info("  Fetching RSS feeds...")
    context = build_rss_context(today)
    log.info(f"  Context: {len(context)} chars")

    prompt = f"""{context}

Based on these RSS headlines, create a daily news briefing. Select the 3-4 most important and interesting stories per category.

Output ONLY valid JSON, nothing before {{ or after }}.

{{"date":"{today}","headline":"A história mais importante do dia, em português","categories":[{{"id":"portugal","name":"Portugal & Política","emoji":"🇵🇹","items":[{{"title":"Título","summary":"3 frases em português: facto + contexto + consequência para alguém em Lisboa.","source":"Público","url":"https://link.com","importance":"high"}}]}},{{"id":"business","name":"Economia & Business","emoji":"💼","items":[]}},{{"id":"mundo","name":"Mundo & Geopolítica","emoji":"🌍","items":[]}},{{"id":"saude","name":"Saúde & Ciência","emoji":"🧬","items":[]}},{{"id":"tech","name":"Tech & IA","emoji":"🤖","items":[]}},{{"id":"desporto","name":"Desporto","emoji":"⚽","items":[]}}]}}

Rules:
- headline: max 120 characters, in Portuguese
- 3-4 items per category
- importance: high | medium | low
- All summaries in Portuguese (2-3 sentences, concise)
- Use real URLs from the headlines above
- Output ONLY the JSON, no other text"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    for attempt in range(3):
        try:
            response = client.messages.create(
                model=MODEL, max_tokens=4096,
                messages=[{"role":"user","content":prompt}],
            )
            text = response.content[0].text
            parsed = extract_json(text)
            if parsed:
                log.info("  ✅ Briefing gerado com sucesso")
                return parsed
            raise RuntimeError(f"JSON inválido:\n{text[:200]}")
        except anthropic.RateLimitError:
            if attempt < 2:
                log.warning(f"  Rate limit — aguardando 15s...")
                time.sleep(15)
            else:
                raise

    raise RuntimeError("Falha após 3 tentativas.")

# ── TELEGRAM FORMATTING ───────────────────────────────────────
IMP_DOT = {"high":"🔴","medium":"🟡","low":"⚪"}
DIVIDER = "─" * 28

def format_messages(briefing: dict) -> list[str]:
    header = (f"📰 <b>BRIEFING DIÁRIO</b>\n<i>{briefing['date']}</i>\n"
              f"{DIVIDER}\n<blockquote>{briefing['headline']}</blockquote>\n")
    blocks, LIMIT = [], 4000
    for cat in briefing["categories"]:
        lines = [f"\n{cat['emoji']} <b>{cat['name'].upper()}</b>"]
        for item in cat["items"]:
            dot = IMP_DOT.get(item.get("importance","low"), "⚪")
            url = item.get("url","")
            src = f'<a href="{url}">{item["source"]}</a>' if url else item["source"]
            lines.append(f'{dot} <b>{item["title"]}</b>\n<i>{item["summary"]}</i>\n↗ {src}')
        blocks.append("\n".join(lines))

    footer = f"\n{DIVIDER}\n⏰ <i>Gerado automaticamente · {briefing['date']}</i>"
    msgs, current = [], header
    for block in blocks:
        if len(current + block) > LIMIT:
            msgs.append(current.strip())
            current = block
        else:
            current += block
    if len(current + footer) <= LIMIT:
        current += footer
    msgs.append(current.strip())
    # Append footer as separate message if it didn't fit
    footer_clean = footer.strip()
    if not any(footer_clean in m for m in msgs):
        msgs.append(footer_clean)
    return msgs

def briefing_to_context(briefing: dict) -> str:
    lines = [f"Briefing do dia {briefing['date']}:"]
    for cat in briefing["categories"]:
        lines.append(f"\n{cat['name']}:")
        for item in cat["items"]:
            lines.append(f"• {item['title']} — {item['summary']} (Fonte: {item['source']})")
    return "\n".join(lines)

# ── BOT HANDLERS ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversation_history.pop(update.effective_user.id, None)
    await update.message.reply_html(
        "👋 Olá! Sou o teu assistente de notícias.\n\n"
        "📰 /briefing — Gerar briefing agora\n"
        "🔄 /hoje — Alias de /briefing\n"
        "💬 <b>Qualquer mensagem</b> → discutir notícias\n\n"
        "O briefing é enviado para o canal todos os dias às 7h."
    )

async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_briefing
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Sem permissão."); return

    msg = await update.message.reply_text("📡 A recolher notícias dos RSS feeds…")
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        briefing = await asyncio.to_thread(generate_briefing, today_label())
        last_briefing = briefing
        await msg.delete()
        for text in format_messages(briefing):
            await update.message.reply_html(text, disable_web_page_preview=True)
    except Exception as e:
        log.error(f"Erro briefing: {e}")
        await msg.edit_text(f"❌ {str(e)[:300]}")

async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    user_text = update.message.text
    if not user_text: return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    if user_id not in conversation_history:
        conversation_history[user_id] = []
    conversation_history[user_id].append({"role":"user","content":user_text})
    if len(conversation_history[user_id]) > MAX_HISTORY:
        conversation_history[user_id] = conversation_history[user_id][-MAX_HISTORY:]

    ctx = briefing_to_context(last_briefing) if last_briefing else "Briefing ainda não gerado hoje."
    system = f"És um assistente de notícias para um profissional português em Lisboa. Respondes sempre em português, de forma direta e analítica. Sê conciso.\n\n{ctx}"

    try:
        def _call():
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            return client.messages.create(model=MODEL, max_tokens=1000,
                system=system, messages=conversation_history[user_id])

        response = await asyncio.to_thread(_call)
        reply = response.content[0].text
        conversation_history[user_id].append({"role":"assistant","content":reply})
        for part in split_message(reply):
            await update.message.reply_text(part)
    except Exception as e:
        log.error(f"Erro chat: {e}")
        await update.message.reply_text(f"❌ {str(e)[:200]}")

# ── SCHEDULER ─────────────────────────────────────────────────
async def send_daily_briefing(bot: Bot):
    global last_briefing
    log.info("⏰ A gerar briefing diário...")
    try:
        briefing = await asyncio.to_thread(generate_briefing, today_label())
        last_briefing = briefing
        for text in format_messages(briefing):
            await bot.send_message(chat_id=TELEGRAM_CHANNEL, text=text,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        log.info("✅ Briefing diário enviado.")
    except Exception as e:
        log.error(f"❌ Erro: {e}")
        try:
            await bot.send_message(chat_id=TELEGRAM_CHANNEL,
                text=f"⚠️ Erro no briefing:\n<code>{str(e)[:300]}</code>",
                parse_mode=ParseMode.HTML)
        except Exception:
            pass

async def post_init(app: Application) -> None:
    scheduler = AsyncIOScheduler(timezone="Europe/Lisbon")
    scheduler.add_job(send_daily_briefing, CronTrigger(hour=7, minute=0, timezone="Europe/Lisbon"),
        args=[app.bot], id="daily_briefing", replace_existing=True)
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    log.info("📅 Scheduler iniciado — 07:00 Lisboa")

async def post_shutdown(app: Application) -> None:
    s = app.bot_data.get("scheduler")
    if s and s.running: s.shutdown(wait=False)

def main():
    log.info("🚀 A iniciar Briefing Diário Bot...")
    app = (Application.builder().token(TELEGRAM_BOT_TOKEN)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("hoje",     cmd_briefing))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat))
    log.info("📡 Bot em polling...")
    app.run_polling(allowed_updates=["message"])

if __name__ == "__main__":
    main()
