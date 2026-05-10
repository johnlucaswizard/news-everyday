"""
Briefing Diário — Railway Cron Job
Gera notícias diárias via Anthropic API e envia para canal Telegram.
"""

import os
import json
import time
import sys
from datetime import datetime

import anthropic
import requests

# ── CONFIG ────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL   = os.environ["TELEGRAM_CHANNEL_ID"]   # ex: @meucanal ou -1001234567890

MODEL = "claude-sonnet-4-20250514"

# ── DATE ─────────────────────────────────────────────────────
WEEKDAYS = ["segunda-feira","terça-feira","quarta-feira","quinta-feira","sexta-feira","sábado","domingo"]
MONTHS   = ["","janeiro","fevereiro","março","abril","maio","junho","julho","agosto","setembro","outubro","novembro","dezembro"]

def today_label():
    n = datetime.now()
    return f"{WEEKDAYS[n.weekday()]}, {n.day} de {MONTHS[n.month]} de {n.year}"

# ── HELPERS ───────────────────────────────────────────────────
def extract_json(text: str) -> dict | None:
    """Extrai JSON de uma string, tolerando fences de markdown."""
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
    """Converte um content block do SDK para dict serializável."""
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump()
    # Fallback manual
    d: dict = {"type": block.type}
    for attr in ("text","id","name","input","tool_use_id","content"):
        if hasattr(block, attr):
            val = getattr(block, attr)
            if val is not None:
                d[attr] = val
    return d


def content_to_str(content) -> str:
    """Converte content de um web_search_tool_result para string."""
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

# ── GENERATE BRIEFING ─────────────────────────────────────────
def generate_briefing(today: str) -> dict:
    """Chama a API da Anthropic com web search e devolve o briefing como dict."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system = f"""You are an elite daily news curator for a senior Portuguese professional in Lisbon. Today is {today}.

Your job: search for today's most significant and intellectually interesting news, prioritising quality journalism over viral content. You are curating for someone who reads The Economist and the Financial Times — someone who values context, consequence, and analytical depth.

## Source hierarchy (search these first)
- **Portugal & Política**: Público, Observador, Expresso, Jornal de Negócios, Jornal Económico, RTP Notícias, Polígrafo
- **Economia & Business**: Financial Times, The Economist, Bloomberg, Reuters, Wall Street Journal, Jornal de Negócios, Dinheiro Vivo
- **Mundo & Geopolítica**: The Economist, Reuters, BBC World, Financial Times, Politico, Foreign Affairs, Le Monde (in English/French)
- **Saúde & Ciência**: The Lancet, Nature, STAT News, BBC Health, New England Journal of Medicine coverage, Science
- **Tech & IA**: MIT Technology Review, Wired, The Information, Financial Times Tech, Ars Technica, Bloomberg Technology
- **Desporto**: ESPN, BBC Sport, Sky Sports, A Bola, Record, O Jogo (for Portuguese sport)

## Selection criteria
- Prefer stories with **lasting consequences** over one-day wonders
- Flag anything that shifts a trend, sets a precedent, or reveals a deeper pattern
- For Portugal: include political, economic, and social developments that affect daily life
- For Business/Economy: macro moves, earnings with systemic impact, policy shifts
- Skip celebrity gossip, pure entertainment, and low-signal viral stories

## Summary quality
Each summary must:
1. State the core fact (what happened)
2. Explain the cause or context (why it happened)
3. Give the "so what" — consequence or significance for a reader in Portugal
Write in clear, direct Portuguese. No jargon, no padding.

After completing all searches, output ONLY a raw JSON object — nothing before or after it. Start with {{ and end with }}.

JSON structure:
{{
  "date": "{today}",
  "headline": "A história mais importante e consequente do dia, em português",
  "categories": [
    {{
      "id": "portugal",
      "name": "Portugal & Política",
      "emoji": "🇵🇹",
      "items": [
        {{
          "title": "Título da notícia",
          "summary": "3 frases: facto + contexto + consequência. Em português.",
          "source": "Nome da publicação (ex: Público, FT, Reuters)",
          "url": "https://link-direto-para-o-artigo.com",
          "importance": "high"
        }}
      ]
    }}
  ]
}}

Include ALL 6 categories (exact ids): portugal · business · mundo · saude · tech · desporto
3-4 items per category. importance: high | medium | low.
All summaries in Portuguese. Output ONLY the JSON."""

    messages = [{
        "role": "user",
        "content": "Search for today's most significant news across all 6 categories. For each category, search the quality sources listed in your instructions. Prioritise stories from the Financial Times, The Economist, Reuters, Público and Observador. After all searches, return only the JSON briefing."
    }]

    for iteration in range(20):
        print(f"  [loop {iteration}] Calling API...")
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=system,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=messages,
        )

        print(f"  [loop {iteration}] stop_reason={response.stop_reason}, blocks={[b.type for b in response.content]}")

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text") and block.type == "text":
                    parsed = extract_json(block.text)
                    if parsed:
                        return parsed
                    raise RuntimeError(f"JSON inválido na resposta:\n{block.text[:500]}")
            raise RuntimeError("end_turn mas sem bloco de texto.")

        if response.stop_reason == "tool_use":
            # Adiciona turno do assistant
            messages.append({
                "role":    "assistant",
                "content": [block_to_dict(b) for b in response.content],
            })

            # Identifica tool_use e resultados de pesquisa
            tool_uses  = [b for b in response.content if b.type in ("tool_use","server_tool_use")]
            ws_results = {b.tool_use_id: b for b in response.content if b.type == "web_search_tool_result"}

            tool_results = [
                {
                    "type":        "tool_result",
                    "tool_use_id": tu.id,
                    "content":     content_to_str(
                        ws_results[tu.id].content if tu.id in ws_results else None
                    ),
                }
                for tu in tool_uses
            ]

            messages.append({
                "role":    "user",
                "content": tool_results or [{"type":"text","text":"Continue and return the final JSON."}],
            })
            continue

        # stop_reason inesperado
        for block in response.content:
            if hasattr(block, "text") and block.type == "text":
                parsed = extract_json(block.text)
                if parsed:
                    return parsed
        raise RuntimeError(f"stop_reason inesperado: {response.stop_reason}")

    raise RuntimeError("Limite de iterações atingido sem resposta final.")

# ── FORMAT FOR TELEGRAM ───────────────────────────────────────
IMP_DOT = {"high": "🔴", "medium": "🟡", "low": "⚪"}

def format_messages(briefing: dict) -> list[str]:
    """Formata o briefing como lista de mensagens Telegram (HTML)."""
    msgs = []

    # Cabeçalho
    msgs.append(
        f"📰 <b>BRIEFING DIÁRIO</b>\n"
        f"<i>{briefing['date']}</i>\n\n"
        f"<blockquote>{briefing['headline']}</blockquote>"
    )

    # Uma mensagem por categoria
    for cat in briefing["categories"]:
        lines = [f"{cat['emoji']} <b>{cat['name']}</b>\n"]
        for item in cat["items"]:
            dot = IMP_DOT.get(item.get("importance","low"), "⚪")
            lines.append(f"{dot} <b>{item['title']}</b>")
            lines.append(f"<i>{item['summary']}</i>")
            url = item.get('url','')
            if url:
                lines.append(f"<a href=\"{url}\">{item['source']}</a>\n")
            else:
                lines.append(f"<code>{item['source']}</code>\n")
        msg = "\n".join(lines)
        # Telegram max 4096 chars — truncate se necessário
        if len(msg) > 4000:
            msg = msg[:3990] + "…"
        msgs.append(msg)

    # Rodapé
    msgs.append(f"⏰ Gerado automaticamente às 07h00 · {briefing['date']}")

    return msgs

# ── SEND TO TELEGRAM ──────────────────────────────────────────
def send_telegram(messages: list[str]) -> None:
    """Envia lista de mensagens para o canal Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for i, text in enumerate(messages):
        resp = requests.post(url, json={
            "chat_id":                  TELEGRAM_CHANNEL,
            "text":                     text,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)

        if not resp.ok:
            raise RuntimeError(f"Telegram error (msg {i+1}): {resp.status_code} — {resp.text}")

        print(f"  Mensagem {i+1}/{len(messages)} enviada ✓")

        if i < len(messages) - 1:
            time.sleep(0.5)   # evita flood do Telegram

# ── MAIN ──────────────────────────────────────────────────────
def main():
    ts = datetime.now().isoformat(timespec="seconds")
    print(f"\n{'='*50}")
    print(f"  BRIEFING DIÁRIO — {ts}")
    print(f"{'='*50}\n")

    today = today_label()
    print(f"Data: {today}\n")

    try:
        print("1/2 — A gerar briefing (pode demorar ~30s)...")
        briefing = generate_briefing(today)
        print(f"     Briefing gerado: {len(briefing['categories'])} categorias\n")

        print("2/2 — A enviar para o Telegram...")
        messages = format_messages(briefing)
        send_telegram(messages)
        print(f"\n✅ Concluído! {len(messages)} mensagens enviadas.")

    except Exception as e:
        print(f"\n❌ ERRO: {e}", file=sys.stderr)
        # Tenta enviar alerta de erro para o canal
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHANNEL, "text": f"⚠️ Briefing Diário — erro às {ts}:\n<code>{str(e)[:300]}</code>", "parse_mode":"HTML"},
                timeout=10,
            )
        except Exception:
            pass
        sys.exit(1)

if __name__ == "__main__":
    main()
