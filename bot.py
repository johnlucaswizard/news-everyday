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

# ID numérico do teu utilizador Telegram (opcional mas recomendado)
# Limita /briefing e chat ao dono do bot. Deixa "" para aceitar toda a gente.
ADMIN_TELEGRAM_ID  = os.environ.get("TELEGRAM_ADMIN_ID", "")

MODEL_BRIEFING = "claude-haiku-4-5-20251001"
MODEL_CHAT     = "claude-haiku-4-5-20251001"

# Histórico de conversa por utilizador (últimas N trocas)
MAX_HISTORY = 12
conversation_history: dict[int, list[dict]] = {}

# Último briefing gerado (contexto para o chat)
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
        return "Search completed."
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
                    parts.append(f"{c.get('title','')}: {c.get('url','')}")
            elif hasattr(c, "type"):
                if c.type == "text":
                    parts.append(getattr(c,"text",""))
        return "\n".join(filter(None, parts))
    return str(content)

def is_admin(user_id: int) -> bool:
    if not ADMIN_TELEGRAM_ID:
        return True  # sem restrição
    return str(user_id) == ADMIN_TELEGRAM_ID.strip()

def split_message(text: str, limit: int = 4000) -> list[str]:
    """Divide texto longo em partes para o Telegram."""
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

# ── BRIEFING GENERATION ───────────────────────────────────────
def generate_briefing(today: str) -> dict:
    """Chama API Anthropic com web search. Corre em thread para não bloquear."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system = f"""You are an elite news curator for a Portuguese professional in Lisbon. Today is {today}.

Search the web for today's most important news. Prioritise quality sources:
- Portugal: Público, Observador, Expresso, Jornal de Negócios
- Business/Economy: Financial Times, The Economist, Bloomberg, Reuters, WSJ
- World: The Economist, Reuters, BBC, Financial Times, Politico
- Health/Science: The Lancet, Nature, STAT News, BBC Health
- Tech/AI: MIT Technology Review, Wired, Ars Technica, FT Tech
- Sport: ESPN, BBC Sport, Sky Sports, A Bola, Record

Select stories with lasting consequences. Skip gossip and low-signal viral content.
Each summary: 3 sentences — fact + context + "so what for someone in Lisbon". In Portuguese.

Output ONLY raw JSON, starting with {{ and ending with }}:
{{"date":"{today}","headline":"frase mais importante do dia em português","categories":[{{"id":"portugal","name":"Portugal & Política","emoji":"🇵🇹","items":[{{"title":"título","summary":"3 frases pt","source":"fonte","url":"https://...","importance":"high"}}]}},{{"id":"business","name":"Economia & Business","emoji":"💼","items":[]}},{{"id":"mundo","name":"Mundo & Geopolítica","emoji":"🌍","items":[]}},{{"id":"saude","name":"Saúde & Ciência","emoji":"🧬","items":[]}},{{"id":"tech","name":"Tech & IA","emoji":"🤖","items":[]}},{{"id":"desporto","name":"Desporto","emoji":"⚽","items":[]}}]}}

3-4 items per category. importance: high|medium|low. All summaries in Portuguese."""
    messages = [{
        "role": "user",
        "content": "Search for today's most significant news. For each of the 6 categories, use the quality sources listed. After all searches, return only the JSON briefing."
    }]

    for iteration in range(8):
        log.info(f"  [loop {iteration}] calling API...")
        import time as _time
        for _attempt in range(4):
            try:
                response = client.messages.create(
                    model=MODEL_BRIEFING,
                    max_tokens=2000,
                    system=system,
                    tools=[{"type": "web_search_20250305", "name": "web_search"}],
                    messages=messages,
                )
                break
            except anthropic.RateLimitError:
                if _attempt < 3:
                    log.warning(f"Rate limit — aguardando 20s (tentativa {_attempt+1}/3)...")
                    _time.sleep(20)
                else:
                    raise
        log.info(f"  [loop {iteration}] stop_reason={response.stop_reason}")

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text") and block.type == "text":
                    parsed = extract_json(block.text)
                    if parsed:
                        return parsed
                    # Model returned narrative instead of JSON — ask explicitly
                    log.info("  end_turn but no JSON — asking again...")
                    messages.append({"role":"assistant","content":[block_to_dict(b) for b in response.content]})
                    messages.append({"role":"user","content":[{"type":"text","text":"Return ONLY the JSON object now. No explanations. Start with { and end with }."}]})
                    break  # continue outer loop
            else:
                raise RuntimeError("end_turn sem bloco de texto.")
            continue

        if response.stop_reason == "tool_use":
            messages.append({"role":"assistant","content":[block_to_dict(b) for b in response.content]})
            tool_uses  = [b for b in response.content if b.type in ("tool_use","server_tool_use")]
            ws_results = {b.tool_use_id: b for b in response.content if b.type == "web_search_tool_result"}
            tool_results = [{
                "type":        "tool_result",
                "tool_use_id": tu.id,
                "content":     content_to_str(ws_results[tu.id].content if tu.id in ws_results else None),
            } for tu in tool_uses]
            messages.append({"role":"user","content": tool_results or [{"type":"text","text":"Continue with the JSON."}]})
            continue

        for block in response.content:
            if hasattr(block, "text") and block.type == "text":
                parsed = extract_json(block.text)
                if parsed:
                    return parsed
        raise RuntimeError(f"stop_reason inesperado: {response.stop_reason}")

    raise RuntimeError("Limite de iterações atingido.")

# ── TELEGRAM FORMATTING ───────────────────────────────────────
IMP_DOT = {"high":"🔴","medium":"🟡","low":"⚪"}
DIVIDER = "─" * 28

def format_messages(briefing: dict) -> list[str]:
    """Máximo 2 mensagens Telegram — todas as categorias num bloco limpo."""
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
            url  = item.get("url","")
            src  = f'<a href="{url}">{item["source"]}</a>' if url else item["source"]
            lines.append(
                f'{dot} <b>{item["title"]}</b>\n'
                f'<i>{item["summary"]}</i>\n'
                f'↗ {src}'
            )
        cat_blocks.append("\n".join(lines))

    footer = f"\n{DIVIDER}\n⏰ <i>Gerado automaticamente · {briefing['date']}</i>"

    LIMIT = 4000
    messages = []
    current = header

    for block in cat_blocks:
        if len(current + block) > LIMIT:
            messages.append(current.strip())
            current = block
        else:
            current += block

    if len(current + footer) <= LIMIT:
        current += footer
    else:
        messages.append(current.strip())
        current = footer.strip()

    messages.append(current.strip())
    return messages

def briefing_to_context(briefing: dict) -> str:
    """Comprime o briefing num bloco de texto para contexto do chat."""
    lines = [f"Briefing do dia {briefing['date']}:"]
    for cat in briefing["categories"]:
        lines.append(f"\n{cat['name']}:")
        for item in cat["items"]:
            lines.append(f"• {item['title']} — {item['summary']} (Fonte: {item['source']})")
    return "\n".join(lines)

# ── BOT HANDLERS ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversation_history.pop(user_id, None)
    await update.message.reply_html(
        "👋 Olá! Sou o teu assistente de notícias diárias.\n\n"
        "📰 /briefing — Gerar briefing agora\n"
        "🔄 /hoje — Alias de /briefing\n"
        "💬 <b>Envia qualquer mensagem</b> para discutir notícias\n\n"
        "O briefing é enviado automaticamente para o canal todos os dias às 7h."
    )

async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_briefing
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Não tens permissão para usar este comando.")
        return

    msg = await update.message.reply_text("🔍 A pesquisar notícias… (~30 segundos)")
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    try:
        today = today_label()
        briefing = await asyncio.to_thread(generate_briefing, today)
        last_briefing = briefing
        await msg.delete()
        for text in format_messages(briefing):
            await update.message.reply_html(text, disable_web_page_preview=True)
        log.info(f"Briefing on-demand enviado para {user_id}")
    except Exception as e:
        log.error(f"Erro no briefing on-demand: {e}")
        await msg.edit_text(f"❌ Erro ao gerar briefing:\n{str(e)[:300]}")

async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Chat livre com contexto do briefing do dia."""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        return  # ignora silenciosamente se não for admin

    user_text = update.message.text
    if not user_text:
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    # Inicializa histórico
    if user_id not in conversation_history:
        conversation_history[user_id] = []

    # Adiciona mensagem do utilizador
    conversation_history[user_id].append({"role":"user","content":user_text})

    # Mantém só as últimas N mensagens
    if len(conversation_history[user_id]) > MAX_HISTORY:
        conversation_history[user_id] = conversation_history[user_id][-MAX_HISTORY:]

    # System prompt com contexto do briefing
    briefing_ctx = briefing_to_context(last_briefing) if last_briefing else "Ainda não foi gerado um briefing hoje."
    system = f"""És um assistente de notícias inteligente para um profissional português em Lisboa. Respondes sempre em português, de forma direta e analítica.

Quando o utilizador faz perguntas sobre notícias, usa o briefing de hoje como ponto de partida.
Se o utilizador pede mais detalhe sobre uma notícia, aprofunda com o teu conhecimento mas avisa se estás a ir além do que foi noticiado.
Sê conciso mas substancial. Sem jargão, sem rodeios.

{briefing_ctx}"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=MODEL_CHAT,
            max_tokens=1200,
            system=system,
            messages=conversation_history[user_id],
        )
        reply = response.content[0].text

        # Adiciona resposta ao histórico
        conversation_history[user_id].append({"role":"assistant","content":reply})

        # Envia (divide se necessário)
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
        today = today_label()
        briefing = await asyncio.to_thread(generate_briefing, today)
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
    """Iniciado dentro do event loop — sítio certo para o scheduler."""
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
        log.info("📅 Scheduler encerrado.")

def main():
    log.info("🚀 A iniciar Briefing Diário Bot...")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Handlers
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("hoje",     cmd_briefing))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat))

    # Polling
    log.info("📡 Bot em polling...")
    app.run_polling(allowed_updates=["message"])

if __name__ == "__main__":
    main()
