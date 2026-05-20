"""
📰 Briefing Diário — Bot Telegram Interativo

Funcionalidades:
  - Envia briefing diário às 7h para o canal
  - /briefing  → gera e envia briefing na hora
  - /hoje      → alias de /briefing
  - Chat livre → discute notícias com IA, com contexto do briefing do dia
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime

import anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update, Bot
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ── LOGGING ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL   = os.environ["TELEGRAM_CHANNEL_ID"]
ADMIN_TELEGRAM_ID  = os.environ.get("TELEGRAM_ADMIN_ID", "")

MODEL = "claude-haiku-4-5-20251001"

MAX_HISTORY = 12
conversation_history: dict[int, list[dict]] = {}
last_briefing: dict | None = None

# ── DATE ─────────────────────────────────────────────────────
WEEKDAYS = ["segunda-feira","terça-feira","quarta-feira","quinta-feira","sexta-feira","sábado","domingo"]
MONTHS   = ["","janeiro","fevereiro","março","abril","maio","junho","julho","agosto","setembro","outubro","novembro","dezembro"]

def today_label() -> str:
    n = datetime.now()
    return f"{WEEKDAYS[n.weekday()]}, {n.day} de {MONTHS[n.month]} de {n.year}"

# ── HELPERS ───────────────────────────────────────────────────
def extract_json(text: str) -> dict | None:
    s = text.strip().replace("```json","").replace("```","").strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b > a:
        try:
            return json.loads(s[a:b+1])
        except json.JSONDecodeError:
            pass
    return None

def block_to_dict(block) -> dict:
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump()
    d: dict = {"type": block.type}
    for attr in ("text","id","name","input","tool_use_id","content"):
        if hasattr(block, attr):
            val = getattr(block, attr)
            if val is not None:
                d[attr] = val
    return d

def content_to_str(content) -> str:
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                t = c.get("type","")
                if t == "text":
                    parts.append(c.get("text",""))
                elif t == "web_search_result":
                    title = c.get("title","")
                    url   = c.get("url","")
                    text  = c.get("text","")[:300]
                    parts.append(f"{title} ({url}): {text}")
            elif hasattr(c, "type") and c.type == "text":
                parts.append(getattr(c, "text", ""))
        return "\n".join(filter(None, parts))
    return str(content)

def is_admin(user_id: int) -> bool:
    if not ADMIN_TELEGRAM_ID:
        return True
    return str(user_id) == ADMIN_TELEGRAM_ID.strip()

def split_message(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                parts.append(current.strip())
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        parts.append(current.strip())
    return parts

def api_call_with_retry(client, **kwargs) -> object:
    """Chama a API com retry automático no rate limit."""
    for attempt in range(4):
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError:
            if attempt < 3:
                wait = 20 * (attempt + 1)
                log.warning(f"Rate limit — aguardando {wait}s (tentativa {attempt+1}/3)...")
                time.sleep(wait)
            else:
                raise

# ── BRIEFING GENERATION (dois fases) ─────────────────────────
def generate_briefing(today: str) -> dict:
    """
    Fase 1: pesquisa com web search, acumula snippets.
    Fase 2: chamada limpa sem ferramentas para formatar JSON.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # ── Fase 1: Pesquisa ──────────────────────────────────────
    search_system = f"""You are a news researcher. Today is {today}.
Search the web for today's most important news across 6 areas:
1. Portugal & politics (Público, Observador, Expresso)
2. Business & economy (FT, Economist, Bloomberg, Reuters, WSJ)
3. World & geopolitics (Economist, Reuters, BBC, Politico)
4. Health & science (Lancet, Nature, STAT News, BBC Health)
5. Tech & AI (MIT Tech Review, Wired, Ars Technica)
6. Sport (ESPN, BBC Sport, A Bola, Record)
Do at least one search per area. Find 3-4 stories per area."""

    messages = [{"role":"user","content":"Search for today's top news in all 6 areas."}]
    collected_snippets: list[str] = []

    for iteration in range(10):
        log.info(f"  [search loop {iteration}]")
        response = api_call_with_retry(
            client,
            model=MODEL,
            max_tokens=1000,
            system=search_system,
            tools=[{"type":"web_search_20250305","name":"web_search"}],
            messages=messages,
        )
        log.info(f"  stop_reason={response.stop_reason}, blocks={[b.type for b in response.content]}")

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason == "tool_use":
            messages.append({"role":"assistant","content":[block_to_dict(b) for b in response.content]})
            tool_uses  = [b for b in response.content if b.type in ("tool_use","server_tool_use")]
            ws_results = {b.tool_use_id: b for b in response.content if b.type == "web_search_tool_result"}

            # Extrai snippets legíveis
            for r in ws_results.values():
                snippet = content_to_str(r.content)
                if snippet:
                    collected_snippets.append(snippet[:1500])

            tool_results = [{
                "type":        "tool_result",
                "tool_use_id": tu.id,
                "content":     content_to_str(ws_results[tu.id].content if tu.id in ws_results else None) or "Done.",
            } for tu in tool_uses]

            messages.append({"role":"user","content": tool_results or [{"type":"text","text":"Continue."}]})

    if not collected_snippets:
        raise RuntimeError("Nenhum resultado de pesquisa recolhido.")

    search_context = "\n\n---\n\n".join(collected_snippets)
    log.info(f"  Recolhidos {len(collected_snippets)} snippets ({len(search_context)} chars)")

    # ── Fase 2: Formatar JSON ─────────────────────────────────
    format_prompt = f"""Here are today's news search results:

{search_context[:10000]}

Based on these results, produce a news briefing. Output ONLY valid JSON — nothing before {{, nothing after }}.

{{"date":"{today}","headline":"A história mais importante do dia, em português","categories":[{{"id":"portugal","name":"Portugal & Política","emoji":"🇵🇹","items":[{{"title":"Título","summary":"3 frases pt: facto+contexto+consequência para alguém em Lisboa.","source":"Público","url":"https://exemplo.com","importance":"high"}}]}},{{"id":"business","name":"Economia & Business","emoji":"💼","items":[]}},{{"id":"mundo","name":"Mundo & Geopolítica","emoji":"🌍","items":[]}},{{"id":"saude","name":"Saúde & Ciência","emoji":"🧬","items":[]}},{{"id":"tech","name":"Tech & IA","emoji":"🤖","items":[]}},{{"id":"desporto","name":"Desporto","emoji":"⚽","items":[]}}]}}

Rules: 3-4 items per category. importance: high|medium|low. All summaries in Portuguese. Include real URLs."""

    log.info("  [format phase] calling API...")
    fmt_response = api_call_with_retry(
        client,
        model=MODEL,
        max_tokens=3500,
        messages=[{"role":"user","content":format_prompt}],
    )

    for block in fmt_response.content:
        if hasattr(block, "text") and block.type == "text":
            parsed = extract_json(block.text)
            if parsed:
                log.info("  ✅ JSON parsed successfully")
                return parsed
            raise RuntimeError(f"JSON inválido na fase de formatação:\n{block.text[:300]}")

    raise RuntimeError("Fase de formatação não devolveu texto.")

# ── TELEGRAM FORMATTING ───────────────────────────────────────
IMP_DOT = {"high":"🔴","medium":"🟡","low":"⚪"}
DIVIDER = "─" * 28

def format_messages(briefing: dict) -> list[str]:
    header = (
        f"📰 <b>BRIEFING DIÁRIO</b>\n"
        f"<i>{briefing['date']}</i>\n"
        f"{DIVIDER}\n"
        f"<blockquote>{briefing['headline']}</blockquote>\n"
    )
    cat_blocks = []
    for cat in briefing["categories"]:
        lines = [f"\n{cat['emoji']} <b>{cat['name'].upper()}</b>"]
        for item in cat["items"]:
            dot = IMP_DOT.get(item.get("importance","low"), "⚪")
            url = item.get("url","")
            src = f'<a href="{url}">{item["source"]}</a>' if url else item["source"]
            lines.append(f'{dot} <b>{item["title"]}</b>\n<i>{item["summary"]}</i>\n↗ {src}')
        cat_blocks.append("\n".join(lines))

    footer = f"\n{DIVIDER}\n⏰ <i>Gerado automaticamente · {briefing['date']}</i>"
    LIMIT  = 4000
    msgs, current = [], header

    for block in cat_blocks:
        if len(current + block) > LIMIT:
            msgs.append(current.strip())
            current = block
        else:
            current += block

    if len(current + footer) <= LIMIT:
        current += footer
    else:
        msgs.append(current.strip())
        current = footer.strip()

    msgs.append(current.strip())
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
        "👋 Olá! Sou o teu assistente de notícias diárias.\n\n"
        "📰 /briefing — Gerar briefing agora\n"
        "🔄 /hoje — Alias de /briefing\n"
        "💬 <b>Qualquer mensagem</b> → discutir notícias\n\n"
        "O briefing é enviado automaticamente para o canal todos os dias às 7h."
    )

async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_briefing
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Não tens permissão.")
        return

    msg = await update.message.reply_text("🔍 A pesquisar notícias… (~30-60s)")
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    try:
        briefing = await asyncio.to_thread(generate_briefing, today_label())
        last_briefing = briefing
        await msg.delete()
        for text in format_messages(briefing):
            await update.message.reply_html(text, disable_web_page_preview=True)
        log.info(f"Briefing on-demand enviado para {user_id}")
    except Exception as e:
        log.error(f"Erro no briefing on-demand: {e}")
        await msg.edit_text(f"❌ Erro: {str(e)[:300]}")

async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Chat livre — não bloqueia o event loop."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    user_text = update.message.text
    if not user_text:
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    if user_id not in conversation_history:
        conversation_history[user_id] = []
    conversation_history[user_id].append({"role":"user","content":user_text})
    if len(conversation_history[user_id]) > MAX_HISTORY:
        conversation_history[user_id] = conversation_history[user_id][-MAX_HISTORY:]

    briefing_ctx = briefing_to_context(last_briefing) if last_briefing else "Ainda não foi gerado um briefing hoje."
    system = f"""És um assistente de notícias para um profissional português em Lisboa. Respondes sempre em português, de forma direta e analítica.
Usa o briefing de hoje como contexto quando relevante. Sê conciso mas substancial.

{briefing_ctx}"""

    try:
        # Corre o SDK síncrono numa thread — não bloqueia o event loop
        def _call():
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            return client.messages.create(
                model=MODEL,
                max_tokens=1200,
                system=system,
                messages=conversation_history[user_id],
            )

        response = await asyncio.to_thread(_call)
        reply = response.content[0].text
        conversation_history[user_id].append({"role":"assistant","content":reply})

        for part in split_message(reply):
            await update.message.reply_text(part)

    except Exception as e:
        log.error(f"Erro no chat: {e}")
        await update.message.reply_text(f"❌ Erro: {str(e)[:200]}")

# ── DAILY SCHEDULER JOB ───────────────────────────────────────
async def send_daily_briefing(bot: Bot):
    global last_briefing
    log.info("⏰ A gerar briefing diário...")
    try:
        briefing = await asyncio.to_thread(generate_briefing, today_label())
        last_briefing = briefing
        for text in format_messages(briefing):
            await bot.send_message(
                chat_id=TELEGRAM_CHANNEL,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        log.info("✅ Briefing diário enviado para o canal.")
    except Exception as e:
        log.error(f"❌ Erro no briefing diário: {e}")
        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHANNEL,
                text=f"⚠️ Erro ao gerar briefing:\n<code>{str(e)[:300]}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

# ── MAIN ──────────────────────────────────────────────────────
async def post_init(app: Application) -> None:
    scheduler = AsyncIOScheduler(timezone="Europe/Lisbon")
    scheduler.add_job(
        send_daily_briefing,
        CronTrigger(hour=7, minute=0, timezone="Europe/Lisbon"),
        args=[app.bot],
        id="daily_briefing",
        replace_existing=True,
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    log.info("📅 Scheduler iniciado — briefing diário às 07:00 (Lisboa)")

async def post_shutdown(app: Application) -> None:
    scheduler = app.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)

def main():
    log.info("🚀 A iniciar Briefing Diário Bot...")
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("hoje",     cmd_briefing))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat))
    log.info("📡 Bot em polling...")
    app.run_polling(allowed_updates=["message"])

if __name__ == "__main__":
    main()
