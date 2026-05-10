# 📰 Briefing Diário — Railway Cron

Envia automaticamente um briefing de notícias para o teu canal Telegram todos os dias às 7h.

---

## 📋 Pré-requisitos

- Conta no [Railway](https://railway.app)
- Conta na [Anthropic](https://console.anthropic.com) (para a API key)
- Telegram instalado

---

## 1️⃣ Criar o Bot do Telegram

1. Abre o Telegram e procura `@BotFather`
2. Envia `/newbot`
3. Escolhe um nome, ex: **Meu Briefing Diário**
4. Escolhe um username, ex: `meu_briefing_bot`
5. O BotFather devolve-te o **token** — guarda-o:
   ```
   1234567890:ABCdefGHIjklMNOpqrSTUVwxyz
   ```

---

## 2️⃣ Criar o Canal e adicionar o Bot

1. No Telegram: **Nova Canal** → dá-lhe um nome e um username (ex: `@meu_briefing`)
2. Nas definições do canal → **Administradores** → Adicionar administrador → procura o teu bot
3. Dá-lhe permissão de **"Publicar mensagens"**

---

## 3️⃣ Obter o Channel ID

**Opção A — canal público** (tem username `@algo`):
- Usa directamente `@meu_briefing` como `TELEGRAM_CHANNEL_ID`

**Opção B — canal privado**:
1. Adiciona o bot `@userinfobot` ao canal como admin temporariamente
2. Envia uma mensagem no canal
3. O bot responde com o ID (ex: `-1001234567890`)
4. Remove o `@userinfobot`

---

## 4️⃣ Deploy no Railway

### Passo 1 — Repositório
Cria um repositório GitHub com estes 3 ficheiros:
```
briefing.py
requirements.txt
railway.toml
```

### Passo 2 — Criar projeto no Railway
1. Vai a [railway.app](https://railway.app) → **New Project**
2. Escolhe **Deploy from GitHub repo**
3. Seleciona o teu repositório

### Passo 3 — Configurar variáveis de ambiente
No Railway, vai ao serviço → **Variables** → adiciona:

| Variável              | Valor                          |
|-----------------------|-------------------------------|
| `ANTHROPIC_API_KEY`   | `sk-ant-api03-...`            |
| `TELEGRAM_BOT_TOKEN`  | `1234567890:ABCdef...`        |
| `TELEGRAM_CHANNEL_ID` | `@meu_briefing` ou `-100123` |

### Passo 4 — Verificar o Cron
O ficheiro `railway.toml` já define `cronSchedule = "0 6 * * *"`.

> ⚠️ **Fuso horário**: Railway usa UTC. `0 6 * * *` = 06h00 UTC = **07h00 Lisboa (inverno/WET)**.
> Em horário de verão (WEST, UTC+2), o envio será às 08h00. Para corrigir:
> - Vai ao Railway → serviço → **Settings** → **Cron Schedule** → muda para `0 5 * * *` no verão.

---

## 5️⃣ Testar manualmente

No Railway, vai ao serviço → **Deploy** → **Trigger Run** (ou usa o Railway CLI):
```bash
railway run python briefing.py
```

---

## 🔧 Troubleshooting

**Erro de JSON inválido**
→ A API às vezes coloca texto extra antes do JSON. O script tenta extrair automaticamente. Se persistir, verifica os logs no Railway.

**Erro de Telegram 403**
→ O bot não tem permissão no canal. Confirma que é administrador com permissão de publicar.

**Erro de Telegram 400 "chat not found"**
→ Verifica o `TELEGRAM_CHANNEL_ID`. Para canais privados usa o ID numérico (`-100...`).

---

## 💰 Custo estimado

- Railway Hobby plan: ~$5/mês (ou usa créditos gratuitos)
- Anthropic API: ~€0.03–0.06 por briefing (com web search)
- Total mensal: **< €7**
