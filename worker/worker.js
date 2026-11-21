// k-watch chat bot – Cloudflare Worker (Telegram webhook + Gemini)
// Secrets (Settings → Variables and Secrets): TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY
// Voliteľné premenné: GEMINI_MODEL, DATA_URL, SITE_URL

const DEFAULT_DATA_URL = "https://tokinko484.github.io/k-watch-7f3/data/ai.enc.json";
const DEFAULT_SITE_URL = "https://tokinko484.github.io/k-watch-7f3/";
let cachedModel = null;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/setup") return setup(url, env);
    if (request.method === "GET") return new Response("ok");
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== await webhookSecret(env)) {
      return new Response("forbidden", { status: 403 });
    }
    const update = await request.json().catch(() => null);
    if (update?.message) ctx.waitUntil(handleMessage(update.message, env).catch(e => console.log("error", e?.stack || e)));
    return new Response("ok");
  },
};

// ------------------------------------------------------------------ Telegram
async function tg(env, method, body) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/${method}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  return r.json();
}

async function webhookSecret(env) {
  const h = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("wh|" + env.TELEGRAM_TOKEN));
  return [...new Uint8Array(h)].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 48);
}

async function setup(url, env) {
  const res = await tg(env, "setWebhook", {
    url: url.origin + "/", secret_token: await webhookSecret(env), allowed_updates: ["message"], drop_pending_updates: true,
  });
  await tg(env, "setMyCommands", { commands: [
    { command: "najnovsie", description: "5 najnovších ponúk" },
    { command: "najlacnejsie", description: "5 najlacnejších ponúk" },
    { command: "stav", description: "Kedy bola posledná kontrola" },
    { command: "pomoc", description: "Čo viem" },
  ] });
  return new Response(JSON.stringify(res), { headers: { "content-type": "application/json" } });
}

async function send(env, chatId, text) {
  for (let i = 0; i < text.length; i += 4000) {
    await tg(env, "sendMessage", { chat_id: chatId, text: text.slice(i, i + 4000), disable_web_page_preview: true });
  }
}

// ------------------------------------------------------------------ dáta
async function loadData(env) {
  const r = await fetch((env.DATA_URL || DEFAULT_DATA_URL) + "?t=" + Date.now(), { cf: { cacheTtl: 0 } });
  if (!r.ok) throw new Error("data " + r.status);
  const enc = await r.json();
  const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  const keyBytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("k-watch-ai|" + env.TELEGRAM_TOKEN));
  const key = await crypto.subtle.importKey("raw", keyBytes, "AES-GCM", false, ["decrypt"]);
  const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(enc.iv) }, key, b64(enc.ct));
  return JSON.parse(new TextDecoder().decode(pt));
}

const fmt = x => [
  `${x.price} € · ${x.rooms} izb.` + (x.area ? ` · ${x.area} m²` : "") + (x.location ? ` · ${x.location}` : ""),
  x.title, x.url,
].join("\n");

const ago = iso => {
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 60) return `pred ${m} min`;
  const h = Math.round(m / 60);
  return h < 24 ? `pred ${h} h` : `pred ${Math.round(h / 24)} d`;
};

// ------------------------------------------------------------------ správy
async function handleMessage(msg, env) {
  const chatId = msg.chat.id;
  const allowed = (env.TELEGRAM_CHAT_ID || "").split(",").map(s => s.trim()).filter(Boolean);
  if (!allowed.includes(String(chatId))) {
    return send(env, chatId, `Tvoje chat ID je: ${chatId}\nPridaj ho do TELEGRAM_CHAT_ID (na GitHube aj v Cloudflare).`);
  }
  const text = (msg.text || "").trim();
  if (!text) return send(env, chatId, "Rozumiem len textovým správam.");
  const cmd = text.split(/[\s@]/)[0].toLowerCase();

  if (cmd === "/start" || cmd === "/pomoc" || cmd === "/help") {
    return send(env, chatId,
      "Ahoj! Pýtaj sa ma na sledované byty, napr.:\n• Ktorý 2-izbák je najlacnejší?\n• Čo pribudlo dnes?\n• Je niečo na Terase do 650 €?\n\n" +
      "Príkazy: /najnovsie, /najlacnejsie, /stav\n\nNové ponuky ti stále posielam automaticky.");
  }

  let data;
  try { data = await loadData(env); }
  catch (e) { return send(env, chatId, "Nepodarilo sa načítať ponuky, skús o chvíľu. (" + e.message + ")"); }
  const active = data.listings.filter(x => x.active !== false);

  if (cmd === "/najnovsie") {
    const l = [...active].sort((a, b) => b.first_seen.localeCompare(a.first_seen)).slice(0, 5);
    return send(env, chatId, l.length ? "🆕 Najnovšie:\n\n" + l.map(x => `${fmt(x)}\n(pridané ${ago(x.first_seen)})`).join("\n\n") : "Žiadne ponuky.");
  }
  if (cmd === "/najlacnejsie") {
    const l = [...active].sort((a, b) => a.price - b.price).slice(0, 5);
    return send(env, chatId, l.length ? "💶 Najlacnejšie:\n\n" + l.map(fmt).join("\n\n") : "Žiadne ponuky.");
  }
  if (cmd === "/stav") {
    const s = data.status || {};
    const src = Object.entries(s.sources || {}).map(([n, v]) => `${v.ok ? "✅" : "❌"} ${n}`).join("\n");
    return send(env, chatId, `Posledná kontrola ${s.checked_at ? ago(s.checked_at) : "?"}\nAktívnych ponúk: ${active.length}\n\n${src}`);
  }

  await tg(env, "sendChatAction", { chat_id: chatId, action: "typing" });
  try {
    const answer = await askGemini(env, text, data);
    return send(env, chatId, answer || "Prepáč, nemám odpoveď.");
  } catch (e) {
    return send(env, chatId, "AI teraz neodpovedá (" + e.message + "). Skús o chvíľu alebo použi /najnovsie.");
  }
}

// ------------------------------------------------------------------ Gemini
async function pickModel(env) {
  if (cachedModel) return cachedModel;
  const r = await fetch("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", {
    headers: { "x-goog-api-key": env.GEMINI_API_KEY },
  });
  const j = await r.json();
  const ok = (j.models || []).filter(m => (m.supportedGenerationMethods || []).includes("generateContent")
    && /flash/.test(m.name) && !/lite|image|tts|live|audio|preview|exp/.test(m.name));
  ok.sort((a, b) => b.name.localeCompare(a.name, undefined, { numeric: true }));
  cachedModel = ok[0]?.name.replace("models/", "") || "gemini-flash-latest";
  return cachedModel;
}

async function callGemini(env, model, system, prompt) {
  return fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY },
    body: JSON.stringify({
      systemInstruction: { parts: [{ text: system }] },
      contents: [{ role: "user", parts: [{ text: prompt }] }],
      generationConfig: { temperature: 0.3, maxOutputTokens: 1500 },
    }),
  });
}

async function askGemini(env, question, data) {
  const now = new Date().toLocaleString("sk-SK", { timeZone: "Europe/Bratislava" });
  const system =
    "Si pomocník páru, ktorý hľadá na prenájom celý 1,5–2-izbový byt v Košiciach do 680 €/mesiac. " +
    "Odpovedaj po slovensky, stručne a vecne, obyčajným textom bez Markdownu (žiadne hviezdičky ani #). " +
    "Pri konkrétnych bytoch vždy uveď cenu, počet izieb, lokalitu a celý odkaz (url). " +
    "Vychádzaj LEN z priložených dát; ak niečo v dátach nie je, povedz to. Nevymýšľaj si. " +
    "active=false znamená, že inzerát zmizol. first_seen je čas, kedy sme inzerát našli (UTC). " +
    `Teraz je ${now}. Web s prehľadom: ${env.SITE_URL || DEFAULT_SITE_URL}`;
  const prompt = `DÁTA (JSON):\n${JSON.stringify(data)}\n\nOTÁZKA: ${question}`;

  let model = env.GEMINI_MODEL || cachedModel || "gemini-flash-latest";
  let r = await callGemini(env, model, system, prompt);
  if (r.status === 404 || r.status === 400) {
    cachedModel = null;
    model = await pickModel(env);
    r = await callGemini(env, model, system, prompt);
  }
  if (r.status === 429) throw new Error("denný limit zadarmo je vyčerpaný");
  const j = await r.json();
  if (!r.ok) throw new Error(j.error?.message?.slice(0, 120) || "HTTP " + r.status);
  cachedModel = model;
  return (j.candidates?.[0]?.content?.parts || []).map(p => p.text || "").join("").trim();
}
