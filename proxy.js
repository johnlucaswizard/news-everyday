// ══════════════════════════════════════════════════════════
//  PROXY BSD → Under Scanner ULTRA
//  Traduz a API Bzzoiro Sports Data (BSD) para o formato
//  API-Football que o dashboard e o engine já esperam.
//  Sem rate limits. Odds embebidas. xG quando disponível.
// ══════════════════════════════════════════════════════════
const https  = require("https");
const http   = require("http");
const { execSync } = require("child_process");
const fs     = require("fs");
const path   = require("path");

const BSD_TOKEN = "0c10761dcaa91c21a76959ee363f4d842c1d03ea";
const BSD_HOST  = "sports.bzzoiro.com";
// Railway define process.env.PORT. Localmente usa 3001.
const PORT      = process.env.PORT || 3001;
// Detecta ambiente cloud (Railway, Render, etc.) — não usa HTTPS próprio lá
const IS_CLOUD  = !!(process.env.PORT || process.env.RAILWAY_ENVIRONMENT || process.env.RENDER);

// Cache simples para reduzir chamadas (BSD não tem rate limit mas poupa latência)
const cache = {};
function cacheGet(key, ttlMs){
  const c = cache[key];
  if(c && (Date.now()-c.ts) < ttlMs) return c.data;
  return null;
}
function cacheSet(key, data){ cache[key] = {data, ts:Date.now()}; }

function generateCert(){
  return new Promise((resolve)=>{
    try{
      execSync(`openssl req -x509 -newkey rsa:2048 -keyout /tmp/key.pem -out /tmp/cert.pem -days 365 -nodes -subj "/CN=localhost" 2>/dev/null`);
      resolve({ key: fs.readFileSync("/tmp/key.pem"), cert: fs.readFileSync("/tmp/cert.pem") });
    }catch(e){ resolve(null); }
  });
}

// ── Chamada genérica à BSD ────────────────────────────────
function bsdCall(apiPath){
  return new Promise((resolve, reject)=>{
    const req = https.request({
      hostname: BSD_HOST,
      path: apiPath,
      method: "GET",
      headers: { "Authorization": `Token ${BSD_TOKEN}` }
    }, (res)=>{
      let body = "";
      res.on("data", c => body += c);
      res.on("end", ()=>{
        try { resolve(JSON.parse(body)); }
        catch(e){ reject(new Error("JSON inválido: "+body.slice(0,100))); }
      });
    });
    req.on("error", reject);
    req.end();
  });
}

// ── Parse de strings "8/15 (53%)" → número absoluto ───────
function parseSlash(str){
  if(typeof str !== "string") return {value:0,total:0,pct:0};
  const m = str.match(/(\d+)\/(\d+)\s*\((\d+)%\)/);
  if(m) return {value:+m[1], total:+m[2], pct:+m[3]};
  return {value:0,total:0,pct:0};
}

// ── Mapa de status BSD → API-Football short ───────────────
function mapStatus(bsdStatus){
  const map = {
    "1st_half":"1H", "2nd_half":"2H", "half_time":"HT",
    "extra_time":"ET", "penalties":"P", "finished":"FT",
    "not_started":"NS", "postponed":"PST", "suspended":"SUSP",
    "break_time":"BT", "live":"2H",
    // Variantes adicionais BSD
    "inprogress":"2H", "in_progress":"2H", "1H":"1H", "2H":"2H",
    "firsthalf":"1H", "secondhalf":"2H", "playing":"2H",
    // Códigos de período BSD (PT): 1T=1ª parte, 2T=2ª parte
    "1T":"1H", "2T":"2H", "INT":"HT", "Intervalo":"HT",
  };
  return map[bsdStatus] || "2H";
}

// ── Traduz live_stats BSD → array statistics API-Football ─
function translateStats(liveStats){
  if(!liveStats || !liveStats.home || !liveStats.away) return null;
  const side = (s)=>{
    const passAcc = s.passes>0 ? Math.round((s.accurate_passes/s.passes)*100) : 0;
    return [
      {type:"Shots on Goal",       value: s.shots_on_target ?? 0},
      {type:"Shots off Goal",      value: s.shots_off_target ?? 0},
      {type:"Total Shots",         value: s.total_shots ?? 0},
      {type:"Blocked Shots",       value: s.blocked_shots ?? 0},
      {type:"Shots insidebox",     value: s.shots_inside_box ?? 0},
      {type:"Shots outsidebox",    value: s.shots_outside_box ?? 0},
      {type:"Fouls",               value: s.fouls ?? 0},
      {type:"Corner Kicks",        value: s.corner_kicks ?? 0},
      {type:"Offsides",            value: 0}, // BSD não tem offsides directo
      {type:"Ball Possession",     value: (s.ball_possession ?? 50)+"%"},
      {type:"Yellow Cards",        value: s.yellow_cards ?? 0},
      {type:"Red Cards",           value: 0}, // extraído dos incidents
      {type:"Goalkeeper Saves",    value: s.goalkeeper_saves ?? 0},
      {type:"Total passes",        value: s.passes ?? 0},
      {type:"Passes accurate",     value: s.accurate_passes ?? 0},
      {type:"Passes %",            value: passAcc+"%"},
      // xG: BSD usa actual_home_xg/actual_away_xg no nível do evento (não aqui)
      {type:"expected_goals",      value: null},
      // ── Campos extra BSD (bónus, lidos pelo engine se existirem) ──
      {type:"Dangerous Attacks",   value: s.touches_in_penalty_area ?? 0}, // proxy: toques na área
      {type:"big_chances",         value: s.big_chances ?? 0},
      {type:"big_chances_missed",  value: s.big_chances_missed ?? 0},
      {type:"Tackles",             value: s.total_tackles ?? s.tackles ?? 0},
      {type:"Goal Kicks",          value: s.goal_kicks ?? 0},
      {type:"Throw Ins",            value: s.throw_ins ?? 0},
    ];
  };
  return [
    {statistics: side(liveStats.home)},
    {statistics: side(liveStats.away)},
  ];
}

// ── Traduz incidents BSD → events API-Football ────────────
function translateEvents(incidents, homeId, awayId){
  if(!Array.isArray(incidents)) return [];
  return incidents.filter(i=>i.type).map(i=>{
    const teamId = i.is_home ? homeId : awayId;
    let type="", detail="";
    if(i.type==="goal"){
      type="Goal";
      detail = i.goal_type==="penalty" ? "Penalty"
             : i.goal_type==="own_goal" ? "Own Goal"
             : "Normal Goal";
    } else if(i.type==="substitution"){
      type="subst"; detail="Substitution";
    } else if(i.type==="card"){
      type="Card";
      detail = i.card_type==="red" ? "Red Card"
             : i.card_type==="yellow" ? "Yellow Card" : "Card";
    } else if(i.type==="var"){
      type="Var"; detail="VAR";
    } else {
      type=i.type; detail=i.text||"";
    }
    return {
      time:{elapsed: i.minute ?? 0, extra: i.added_time||null},
      team:{id: teamId, name: i.is_home?"home":"away"},
      player:{id: i.player_id||null, name: i.player||i.player_in||""},
      assist:{id: i.player_out_id||null, name: i.player_out||""},
      type, detail,
    };
  });
}

// ── Conta red cards dos incidents para corrigir as stats ──
function countReds(incidents, isHome){
  if(!Array.isArray(incidents)) return 0;
  return incidents.filter(i=>i.type==="card" && i.card_type==="red" && i.is_home===isHome).length;
}

// ── Traduz 1 jogo BSD → fixture API-Football ──────────────
function translateFixture(ev){
  const homeId = ev.home_team_obj?.id ?? 0;
  const awayId = ev.away_team_obj?.id ?? 0;
  return {
    fixture:{
      id: ev.id,
      date: ev.event_date || null,
      referee: ev.referee || null,
      // Status: prefere 'period' (1H/2H) que é mais fiável que 'status' (inprogress)
      status:{ short: mapStatus(ev.period || ev.status), elapsed: ev.current_minute ?? 0 },
      venue:{ city: ev.venue?.city || ev.home_team_obj?.venue?.city || "" },
    },
    league:{
      id: ev.league?.id ?? 0,
      name: ev.league?.name || "",
      country: ev.league?.country || "",
      season: ev.season?.year ?? ev.league?.current_season?.year ?? 2026,
    },
    teams:{
      home:{ id: homeId, name: ev.home_team || "" },
      away:{ id: awayId, name: ev.away_team || "" },
    },
    goals:{ home: ev.home_score ?? 0, away: ev.away_score ?? 0 },
    score:{ halftime:{ home: ev.home_score_ht ?? null, away: ev.away_score_ht ?? null } },
    // ── Dados extra BSD anexados (odds, coach, weather) ──
    _bsd:{
      odds:{
        under15: ev.odds_under_15, under25: ev.odds_under_25, under35: ev.odds_under_35,
        over15: ev.odds_over_15, over25: ev.odds_over_25,
        bttsNo: ev.odds_btts_no, bttsYes: ev.odds_btts_yes,
        home: ev.odds_home, draw: ev.odds_draw, away: ev.odds_away,
      },
      xgHome: ev.actual_home_xg ?? ev.home_xg_live,
      xgAway: ev.actual_away_xg ?? ev.away_xg_live,
      homeCoach: ev.home_coach, awayCoach: ev.away_coach,
      weather:{ code: ev.weather_code, wind: ev.wind_speed, temp: ev.temperature_c },
      incidents: ev.incidents || [],
    },
  };
}

function sendJSON(res, obj){
  res.writeHead(200, {"Content-Type":"application/json","Access-Control-Allow-Origin":"*"});
  res.end(JSON.stringify(obj));
}

// ── JOURNAL — persiste o btLog num ficheiro no disco ───────
// Protege os dados de validação contra limpezas do browser.
const JOURNAL_FILE = path.join(__dirname, "trade-journal.json");
function journalHandler(req, res){
  if(req.method === "GET"){
    try{
      if(fs.existsSync(JOURNAL_FILE)){
        res.writeHead(200, {"Content-Type":"application/json","Access-Control-Allow-Origin":"*"});
        res.end(fs.readFileSync(JOURNAL_FILE));
      } else {
        res.writeHead(200, {"Content-Type":"application/json","Access-Control-Allow-Origin":"*"});
        res.end("{}");
      }
    }catch(e){ res.writeHead(500,{"Access-Control-Allow-Origin":"*"}); res.end("{}"); }
    return;
  }
  if(req.method === "POST"){
    let body="";
    req.on("data", c=>body+=c);
    req.on("end", ()=>{
      try{
        JSON.parse(body); // valida
        fs.writeFileSync(JOURNAL_FILE, body);
        res.writeHead(200,{"Content-Type":"application/json","Access-Control-Allow-Origin":"*"});
        res.end('{"ok":true}');
      }catch(e){
        res.writeHead(400,{"Access-Control-Allow-Origin":"*"});
        res.end('{"ok":false}');
      }
    });
    return;
  }
  res.writeHead(405); res.end();
}

// ── TELEGRAM RELAY ─────────────────────────────────────────
function telegramRelay(req, res){
  let body="";
  req.on("data", c=>body+=c);
  req.on("end", ()=>{
    let p; try{ p=JSON.parse(body); }catch{ res.writeHead(400); res.end('{"ok":false}'); return; }
    const {token, chat_id, text} = p;
    if(!token||!chat_id||!text){ res.writeHead(400); res.end('{"ok":false,"error":"missing"}'); return; }
    const tgBody = JSON.stringify({chat_id, text, parse_mode:"HTML"});
    const tgReq = https.request({
      hostname:"api.telegram.org", path:`/bot${token}/sendMessage`, method:"POST",
      headers:{"Content-Type":"application/json","Content-Length":Buffer.byteLength(tgBody)}
    }, tgRes=>{
      let d=""; tgRes.on("data",c=>d+=c);
      tgRes.on("end",()=>{
        res.writeHead(200,{"Content-Type":"application/json","Access-Control-Allow-Origin":"*"});
        res.end(d);
        try{ const j=JSON.parse(d); console.log(j.ok?`📤 Telegram: "${text.slice(0,40)}..."`:`⚠ Telegram: ${j.description}`);}catch{}
      });
    });
    tgReq.on("error",e=>{ console.log(`⚠ Telegram falhou: ${e.message}`); res.writeHead(500,{"Access-Control-Allow-Origin":"*"}); res.end('{"ok":false}'); });
    tgReq.write(tgBody); tgReq.end();
  });
}

// ══════════════════════════════════════════════════════════
//  HANDLER
// ══════════════════════════════════════════════════════════
async function handler(req, res){
  res.setHeader("Access-Control-Allow-Origin","*");
  res.setHeader("Access-Control-Allow-Headers","*");
  res.setHeader("Access-Control-Allow-Methods","GET, POST, OPTIONS");
  if(req.method==="OPTIONS"){ res.writeHead(204); res.end(); return; }

  const parsed = new URL(req.url, "https://localhost");
  const pathname = parsed.pathname;

  try {
    // ── JOURNAL — persistência do backtest ───────────────
    if(pathname==="/journal") return journalHandler(req, res);

    // ── TELEGRAM ─────────────────────────────────────────
    if(pathname==="/telegram" && req.method==="POST") return telegramRelay(req, res);

    // ── LIVE — lista de jogos ao vivo ────────────────────
    if(pathname==="/live"){
      const cached = cacheGet("live", 25000); // 25s cache (BSD cacheia 30s)
      if(cached) return sendJSON(res, cached);
      const d = await bsdCall("/api/live/");
      // Cacheia os dados CRUS de cada jogo por id — o /live/ tem xG+stats+incidents
      // que o /events/{id}/ não traz (xG vem null lá). O /stats/ usa isto.
      (d.results||[]).forEach(g=>cacheSet(`liveraw_${g.id}`, g));
      const fixtures = (d.results||[]).map(translateFixture);
      const out = {response: fixtures, results: fixtures.length};
      cacheSet("live", out);
      console.log(`✓ live — ${fixtures.length} jogos`);
      return sendJSON(res, out);
    }

    // ── STATS de um jogo ─────────────────────────────────
    const statsMatch = pathname.match(/^\/stats\/(\d+)$/);
    if(statsMatch){
      const id = statsMatch[1];
      // Preferir os dados crus do /live/ (têm xG correcto). Só ir ao
      // /events/ se não houver dados em cache do /live/.
      const liveRaw = cacheGet(`liveraw_${id}`, 30000);
      const ck = `ev_${id}`;
      let ev = cacheGet(ck, 25000);
      if(!ev){ ev = await bsdCall(`/api/events/${id}/`); cacheSet(ck, ev); }
      // Combina: usa live_stats do /live/ se existir (mais fresco), senão do /events/
      const liveStats = liveRaw?.live_stats || ev.live_stats;
      const incidents = liveRaw?.incidents || ev.incidents;
      const stats = translateStats(liveStats);
      // Corrige red cards a partir dos incidents
      if(stats && incidents){
        const redsH = countReds(incidents, true);
        const redsA = countReds(incidents, false);
        const rcH = stats[0].statistics.find(s=>s.type==="Red Cards"); if(rcH) rcH.value=redsH;
        const rcA = stats[1].statistics.find(s=>s.type==="Red Cards"); if(rcA) rcA.value=redsA;
      }
      // xG: PRIORIDADE ao /live/ (home_xg_live), que tem valor mesmo no Mundial
      if(stats){
        const xgH = liveRaw?.home_xg_live ?? ev.actual_home_xg ?? ev.home_xg_live;
        const xgA = liveRaw?.away_xg_live ?? ev.actual_away_xg ?? ev.away_xg_live;
        const exgH = stats[0].statistics.find(s=>s.type==="expected_goals"); if(exgH) exgH.value = xgH;
        const exgA = stats[1].statistics.find(s=>s.type==="expected_goals"); if(exgA) exgA.value = xgA;
      }
      const xgInfo = (liveRaw?.home_xg_live ?? ev.actual_home_xg ?? ev.home_xg_live) ?? "null";
      console.log(`✓ stats ${id} — xG:${xgInfo} · shots:${liveStats?.home?.total_shots ?? "?"} · poss:${liveStats?.home?.ball_possession ?? "?"}% ${liveRaw?"(via /live/)":"(via /events/)"}`);
      return sendJSON(res, {response: stats || [], results: stats?2:0});
    }

    // ── RESULT — jogo terminado (para btSettle) ──────────
    const resultMatch = pathname.match(/^\/result\/(\d+)$/);
    if(resultMatch){
      const id = resultMatch[1];
      const ev = await bsdCall(`/api/events/${id}/`);
      const fixture = translateFixture(ev);
      // Anexa events para o btSettle contar golos pós-entrada
      fixture.events = translateEvents(ev.incidents, fixture.teams.home.id, fixture.teams.away.id);
      return sendJSON(res, {response:[fixture], results:1});
    }

    // ── EVENTS — incidentes de um jogo ───────────────────
    const eventsMatch = pathname.match(/^\/events\/(\d+)$/);
    if(eventsMatch){
      const id = eventsMatch[1];
      const ev = await bsdCall(`/api/events/${id}/`);
      const homeId = ev.home_team_obj?.id ?? 0;
      const awayId = ev.away_team_obj?.id ?? 0;
      const events = translateEvents(ev.incidents, homeId, awayId);
      return sendJSON(res, {response: events, results: events.length});
    }

    // ── STANDINGS — classificação da liga ────────────────
    const standings = pathname.match(/^\/standings\/(\d+)\/(\d+)$/);
    if(standings){
      const leagueId = standings[1];
      const ck = `stand_${leagueId}`;
      let cached = cacheGet(ck, 600000); // 10min cache (standings mudam pouco)
      if(cached) return sendJSON(res, cached);
      try{
        const d = await bsdCall(`/api/leagues/${leagueId}/standings/`);
        const rows = d.standings || [];
        // Traduz para o formato API-Football: response[0].league.standings[0][]
        const table = rows.map(r=>({
          rank: r.position,
          team:{ id: r.team_id, name: r.team },
          points: r.pts,
          goalsDiff: r.gd,
          all:{
            played: r.played, win: r.won, draw: r.drawn, lose: r.lost,
            goals:{ for: r.gf, against: r.ga },
          },
          form: r.form || "",
          // Bónus BSD: xG da época por equipa (a API-Football não dava)
          xgFor: r.xgf, xgAgainst: r.xga, xgGames: r.xg_games,
        }));
        const out = { response:[{ league:{ id:+leagueId, standings:[table] } }], results: table.length };
        cacheSet(ck, out);
        console.log(`✓ standings liga ${leagueId} — ${table.length} equipas`);
        return sendJSON(res, out);
      }catch(e){
        console.log(`⚠ standings ${leagueId}: ${e.message}`);
        return sendJSON(res, {response:[], results:0});
      }
    }

    // ── FORM — derivada da string "form" das standings ───
    const form = pathname.match(/^\/form\/(\d+)\/(\d+)$/);
    if(form){
      const teamId = +form[1];
      const leagueId = form[2];
      try{
        const d = await bsdCall(`/api/leagues/${leagueId}/standings/`);
        const row = (d.standings||[]).find(r=>r.team_id===teamId);
        if(!row) return sendJSON(res, {response:[], results:0});
        // Constrói objeto de forma a partir dos agregados da época
        const played = Math.max(1, row.played||1);
        const formResp = {
          team:{ id: teamId },
          // Taxas estimadas a partir dos agregados (gf/ga por jogo)
          all:{
            played: row.played,
            goalsFor: row.gf, goalsAgainst: row.ga,
            avgFor: +(row.gf/played).toFixed(2),
            avgAgainst: +(row.ga/played).toFixed(2),
            // Clean sheets estimado: jogos sem sofrer (proxy via ga baixo)
            cleanSheets: row.ga<=played*0.5 ? 0.5 : +(Math.max(0,(played-row.ga)/played)).toFixed(2),
            form: row.form||"",
            xgFor: row.xgf, xgAgainst: row.xga,
          },
        };
        return sendJSON(res, {response:[formResp], results:1, _bsdForm:formResp.all});
      }catch(e){
        return sendJSON(res, {response:[], results:0});
      }
    }

    // ── H2H — cruza jogos terminados das 2 equipas ───────
    const h2h = pathname.match(/^\/h2h\/(\d+)\/(\d+)$/);
    if(h2h){
      const teamA = +h2h[1], teamB = +h2h[2];
      const ck = `h2h_${teamA}_${teamB}`;
      let cached = cacheGet(ck, 3600000); // 1h cache
      if(cached) return sendJSON(res, cached);
      try{
        // BSD não tem filtro por equipa nem endpoint h2h directo.
        // Busca jogos terminados recentes e filtra os confrontos diretos.
        const d = await bsdCall(`/api/events/?status=finished&limit=200`);
        const all = d.results || [];
        const direct = all.filter(m=>{
          const h = m.home_team_obj?.id, a = m.away_team_obj?.id;
          return (h===teamA&&a===teamB)||(h===teamB&&a===teamA);
        });
        // Traduz para fixtures API-Football (o dashboard calcula as stats h2h)
        const fixtures = direct.map(translateFixture);
        const out = { response: fixtures, results: fixtures.length };
        cacheSet(ck, out);
        if(fixtures.length) console.log(`✓ h2h ${teamA}v${teamB} — ${fixtures.length} confrontos`);
        return sendJSON(res, out);
      }catch(e){
        return sendJSON(res, {response:[], results:0});
      }
    }

    // ── Endpoints ainda não mapeados (engine lida com null) ──
    if(pathname.match(/^\/(referee|players|lineups|predict|injuries)\//)){
      return sendJSON(res, {response:[], results:0});
    }

    // ── Serve o dashboard HTML ───────────────────────────
    if(pathname==="/" || pathname==="/dashboard" || pathname==="/index.html"){
      const candidates = [
        path.join(__dirname,"under-scanner-ultra.html"),
        path.join(__dirname,"ultra3.html"),
      ];
      for(const f of candidates){
        if(fs.existsSync(f)){
          res.writeHead(200,{"Content-Type":"text/html; charset=utf-8"});
          res.end(fs.readFileSync(f)); return;
        }
      }
      res.writeHead(404); res.end("HTML não encontrado na pasta do proxy."); return;
    }

    // Fallback
    return sendJSON(res, {response:[], results:0});

  } catch(e){
    console.log(`⚠ erro ${pathname}: ${e.message}`);
    res.writeHead(500,{"Content-Type":"application/json","Access-Control-Allow-Origin":"*"});
    res.end(JSON.stringify({error:e.message, response:[], results:0}));
  }
}

// ══════════════════════════════════════════════════════════
//  WORKER 24/7 — scanner autónomo no servidor (Railway)
//  Corre sem browser. Busca jogos, avalia entradas, dispara
//  Telegram. Versão essencial do motor (sem histórico de delta).
// ══════════════════════════════════════════════════════════
const TG_TOKEN = process.env.TG_TOKEN || "8651086431:AAFoSQi7BoqpdwjFAzj5Ii-NU5_LzyRtat8";
const TG_CHAT  = process.env.TG_CHAT  || "729107042";
const WORKER_ON = IS_CLOUD || process.env.WORKER === "1"; // só na cloud por defeito
const SCAN_INTERVAL_MS = 60000; // 1 min
const sentAlerts = {}; // { "fixtureId_market": timestamp } — evita repetir

function tgSend(text){
  return new Promise((resolve)=>{
    const body = JSON.stringify({chat_id:TG_CHAT, text, parse_mode:"HTML"});
    const r = https.request({
      hostname:"api.telegram.org", path:`/bot${TG_TOKEN}/sendMessage`, method:"POST",
      headers:{"Content-Type":"application/json","Content-Length":Buffer.byteLength(body)}
    }, res=>{ let d=""; res.on("data",c=>d+=c); res.on("end",()=>{
      try{const j=JSON.parse(d); console.log(j.ok?`📤 Worker Telegram: ${text.slice(0,45)}`:`⚠ TG: ${j.description}`);}catch{}
      resolve();
    });});
    r.on("error",e=>{console.log(`⚠ Worker TG erro: ${e.message}`);resolve();});
    r.write(body); r.end();
  });
}

// Avalia UM jogo e devolve sinal de entrada (ou null) — motor essencial
function evalEntry(ev, stats){
  const total = (ev.home_score??0)+(ev.away_score??0);
  const min = ev.current_minute ?? 0;
  const gH = ev.home_score??0, gA = ev.away_score??0;
  if(total>=3 || min<55 || min>95) return null;

  // MERCADO (mesmas regras do dashboard)
  let market=null;
  if(total===0&&min>=73)      market="Under 0.5";
  else if(total===0&&min>=57) market="Under 1.5";
  else if(total===1&&min>=69) market="Under 1.5";
  else if(total===1&&min>=55) market="Under 2.5";
  else if(total===2&&min>=72) market="Under 2.5";
  if(!market) return null;

  const s = stats?.live_stats;
  const sh = s?.home||{}, sa = s?.away||{};
  const shotsOn = (sh.shots_on_target??0)+(sa.shots_on_target??0);
  const shotsTot = (sh.total_shots??0)+(sa.total_shots??0);
  const xgH = ev.actual_home_xg ?? ev.home_xg_live;
  const xgA = ev.actual_away_xg ?? ev.away_xg_live;
  const xgTot = (xgH!=null&&xgA!=null) ? xgH+xgA : null;
  const hasStats = shotsTot>0 || (sh.passes??0)+(sa.passes??0)>0;

  // SCORE essencial (0-100) — espelha os pesos principais do motor
  let score=0;
  // Estado do marcador
  if(total===0) score += min>=80?44 : min>=73?40 : min>=67?36 : min>=57?32 : 28;
  else if(total===1) score += min>=80?30 : min>=73?26 : 22;
  else if(total===2) score += min>=80?18 : 14;
  // Minuto (tempo restante)
  const minsLeft = Math.max(0, 93-min);
  if(minsLeft<=5) score+=16; else if(minsLeft<=10) score+=11; else if(minsLeft<=18) score+=6;

  const hasXg = (xgTot!=null);
  if(hasXg){
    // xG disponível: usa-o (sinal forte)
    if(xgTot<0.6) score+=16; else if(xgTot<1.0) score+=10; else if(xgTot<1.5) score+=4;
    else if(xgTot>=2.2) score-=14; else if(xgTot>=1.8) score-=8;
  }
  // Remates à baliza — peso REFORÇADO quando não há xG (jogos de seleções)
  if(hasStats){
    if(hasXg){
      if(shotsOn<=2) score+=12; else if(shotsOn<=4) score+=6; else if(shotsOn>=9) score-=10;
      if(shotsTot<=6) score+=6; else if(shotsTot>=20) score-=8;
    } else {
      // Sem xG: remates tornam-se o proxy principal de perigo → mais peso
      if(shotsOn<=3) score+=18; else if(shotsOn<=5) score+=12; else if(shotsOn<=7) score+=4;
      else if(shotsOn>=11) score-=12; else if(shotsOn>=9) score-=6;
      if(shotsTot<=8) score+=10; else if(shotsTot<=12) score+=4; else if(shotsTot>=22) score-=8;
      // Remates à baliza por minuto (intensidade ofensiva)
      const shotsOnRate = shotsOn/Math.max(1,min);
      if(shotsOnRate<0.05) score+=8; // <1 remate à baliza cada 20min = jogo morto
    }
  }
  // Posse equilibrada (jogo morto) vs dominância
  const posH = sh.ball_possession??50;
  if(Math.abs(posH-50)<=10 && total===0 && min>=70) score+=6;
  // Cantos baixos = poucas situações de perigo (proxy extra sem xG)
  if(!hasXg && hasStats){
    const cornTot=(sh.corner_kicks??0)+(sa.corner_kicks??0);
    if(cornTot<=4 && min>=70) score+=5;
  }
  score = Math.max(0, Math.min(100, score));

  // THRESHOLD (mesma escala do dashboard)
  let threshold=76;
  if(min>=83) threshold=52; else if(min>=78) threshold=58;
  else if(min>=73) threshold=63; else if(min>=68) threshold=68;
  if(total===2) threshold=Math.min(threshold+10,85);
  // Sem xG: penalização REDUZIDA (+3 em vez de +6) — senão jogos de
  // seleções nunca disparam. Compensámos já com mais peso nos remates.
  if(xgTot==null) threshold=Math.min(threshold+3,88);
  if(!hasStats) threshold=Math.min(threshold+6,92);

  // GATE U2.5 — só com convergência forte (igual ao dashboard)
  if(market==="Under 2.5"){
    const scoreOK = score>=80;
    const xgOK = xgTot!=null ? xgTot<1.3 : shotsOn<=6;
    // histórico não disponível no worker → exige score+xG fortes
    if(!(scoreOK && xgOK)) return null; // worker é conservador no U2.5
  }

  if(score < threshold) return null;

  const odd = market==="Under 1.5"?ev.odds_under_15 : market==="Under 2.5"?ev.odds_under_25 : null;
  return { market, score, threshold, min, odd, total, gH, gA,
           home:ev.home_team, away:ev.away_team,
           league:ev.league?.name||"", country:ev.league?.country||"",
           u25:market==="Under 2.5" };
}

async function scanCycle(){
  try{
    const live = await bsdCall("/api/live/");
    const games = live.results||[];
    let evaluated=0, signals=0;
    // Log de visibilidade: mostra TODOS os jogos live (mesmo não-candidatos)
    if(games.length>0){
      const resumo = games.map(g=>{
        const t=(g.home_score??0)+(g.away_score??0);
        const m=g.current_minute??0;
        return `${g.home_team} ${g.home_score??0}-${g.away_score??0} ${g.away_team} (${m}')`;
      }).join(" | ");
      console.log(`🔍 ${games.length} jogos live: ${resumo}`);
    } else {
      console.log(`🔍 0 jogos live na BSD agora`);
    }
    for(const g of games){
      const total=(g.home_score??0)+(g.away_score??0);
      const min=g.current_minute??0;
      // pré-filtro barato antes de buscar stats
      if(total>=3 || min<55 || min>95){
        if(min<55 && min>0) console.log(`   ⏳ ${g.home_team} vs ${g.away_team}: min ${min} — espera min 55+`);
        continue;
      }
      evaluated++;
      // O /live/ já traz xG (home_xg_live), live_stats e incidents.
      // Buscamos /events/ APENAS para as odds (que só lá existem).
      let detail = g; // base: dados do /live/ (com xG correcto!)
      try{
        const odds = await bsdCall(`/api/events/${g.id}/`);
        // Combina: stats+xG do /live/ + odds do /events/
        detail = Object.assign({}, g, {
          odds_under_15: odds.odds_under_15,
          odds_under_25: odds.odds_under_25,
          odds_btts_no: odds.odds_btts_no,
          // Mantém xG do /live/ se o /events/ vier null (caso Mundial)
          home_xg_live: g.home_xg_live ?? odds.actual_home_xg ?? odds.home_xg_live,
          away_xg_live: g.away_xg_live ?? odds.actual_away_xg ?? odds.away_xg_live,
        });
      }catch{ /* sem odds, usa só dados do /live/ */ }
      const sig = evalEntry(detail, detail);
      if(!sig) continue;
      const key = `${g.id}_${sig.market}`;
      if(sentAlerts[key]) continue; // já enviado este sinal
      sentAlerts[key] = Date.now();
      signals++;
      const tag = sig.u25 ? " 🎯 SELECTIVO" : "";
      await tgSend(
        `⚡ <b>ENTRY SIGNAL</b>${tag}\n`+
        `${sig.home} vs ${sig.away}\n`+
        `🏆 ${sig.league} · ${sig.country}\n`+
        `📊 ${sig.market}${sig.odd?" @ "+(+sig.odd).toFixed(2):""} · min ${sig.min}' · ${sig.gH}-${sig.gA}\n`+
        `💪 Confiança: ${sig.score}/100 (min ${sig.threshold})\n`+
        `🤖 alerta automático (servidor 24/7)`
      );
    }
    // limpa alertas antigos (>3h) para libertar memória
    const cut=Date.now()-3*3600*1000;
    for(const k in sentAlerts) if(sentAlerts[k]<cut) delete sentAlerts[k];
    if(evaluated>0) console.log(`   → ${evaluated} candidatos avaliados, ${signals} sinais enviados`);
  }catch(e){ console.log(`⚠ scan erro: ${e.message}`); }
}

function startServer(options){
  if(options && !IS_CLOUD){
    // LOCAL: HTTPS com certificado self-signed em localhost:3001
    https.createServer(options, handler).listen(PORT, ()=>{
      console.log(`✅ Proxy BSD HTTPS a correr em https://localhost:${PORT}`);
      console.log(`   → Abre no browser: https://localhost:${PORT}`);
      console.log(`   → Fonte: Bzzoiro Sports Data (sem rate limits)`);
      console.log(`   → Telegram relay em /telegram`);
    });
  } else {
    // CLOUD (Railway): HTTP simples — o Railway trata do HTTPS externamente
    http.createServer(handler).listen(PORT, ()=>{
      console.log(`✅ Proxy BSD a correr na porta ${PORT} (${IS_CLOUD?"CLOUD":"local HTTP"})`);
      console.log(`   → Fonte: Bzzoiro Sports Data (sem rate limits)`);
      console.log(`   → Telegram relay em /telegram`);
    });
  }
}

// No cloud não gera certificado (poupa tempo de arranque e evita falha do openssl)
if(IS_CLOUD){
  startServer(null);
} else {
  generateCert().then(startServer);
}

// ── Arranca o worker 24/7 ──────────────────────────────────
if(WORKER_ON){
  console.log(`🤖 Worker 24/7 ACTIVO — scan a cada ${SCAN_INTERVAL_MS/1000}s, alertas Telegram automáticos`);
  console.log(`   Chat: ${TG_CHAT} · mercados: U0.5, U1.5, U2.5(selectivo)`);
  setTimeout(scanCycle, 5000);           // primeiro scan 5s após arranque
  setInterval(scanCycle, SCAN_INTERVAL_MS);
} else {
  console.log(`ℹ Worker desligado (local). Para activar: WORKER=1 node proxy.js`);
}
