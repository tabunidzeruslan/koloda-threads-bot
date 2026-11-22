// Koloda Telegram relay (Cloudflare Worker, free plan).
// Telegram sends every message/button press here (webhook). The Worker stores it in KV and wakes the
// GitHub Actions bot at once (workflow_dispatch), so the bot answers in 30-60 s without running all day.
// The bot then pulls the stored updates from /updates and acknowledges them with /ack.
// It is also the bot's clock: a Cloudflare cron trigger (every 10 min, 06:00-23:59 Kyiv) starts a normal run, because
// GitHub's own schedule is best-effort and may not fire on a new repo.
// Secrets (set by the deploy workflow): WEBHOOK_SECRET, PULL_SECRET, DISPATCH_TOKEN, REPO.

const DEBOUNCE_MS = 40_000;
const CLOCK_SKIP_MS = 5 * 60_000;   // a run started by a button press in the last 5 min already did the work

export default {
  async scheduled(event, env, ctx) {
    const last = Number((await env.Q.get("last_wake")) || 0);
    if (Date.now() - last < CLOCK_SKIP_MS) return;
    ctx.waitUntil(wake(env, true));
  },

  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    if (url.pathname === "/health") return new Response("ok");

    if (url.pathname === "/tg" && req.method === "POST") {
      if (req.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      const upd = await req.json();
      if (typeof upd.update_id === "number") {
        await env.Q.put(`u:${String(upd.update_id).padStart(12, "0")}`, JSON.stringify(upd),
                        { expirationTtl: 3 * 86400 });
        await wake(env);
        // show "друкує…" at the top of the chat while the bot starts, so the owner sees it is working
        const chat = (upd.message && upd.message.chat && upd.message.chat.id) ||
                     (upd.callback_query && upd.callback_query.message && upd.callback_query.message.chat.id);
        if (chat && env.TG_TOKEN && ctx) ctx.waitUntil(typing(env, chat));
        // stop the button spinner at once: Telegram accepts a method call as the webhook response
        if (upd.callback_query) {
          return Response.json({ method: "answerCallbackQuery", callback_query_id: upd.callback_query.id,
                                 text: "Прийнято, виконую. Відповідь за пів хвилини" });
        }
      }
      return new Response("ok");
    }

    const auth = req.headers.get("Authorization") === `Bearer ${env.PULL_SECRET}`;
    if (!auth) return new Response("forbidden", { status: 403 });

    if (url.pathname === "/updates") {
      const list = await env.Q.list({ prefix: "u:", limit: 100 });
      const out = [];
      for (const k of list.keys) {
        const v = await env.Q.get(k.name);
        if (v) out.push(JSON.parse(v));
      }
      return Response.json(out);
    }
    if (url.pathname === "/ack" && req.method === "POST") {
      const { ids } = await req.json();
      for (const id of ids || []) await env.Q.delete(`u:${String(id).padStart(12, "0")}`);
      return new Response("ok");
    }
    if (url.pathname === "/wake" && req.method === "POST") {
      await wake(env, true);
      return new Response("ok");
    }
    return new Response("not found", { status: 404 });
  },
};

async function typing(env, chat) {
  for (let i = 0; i < 6; i++) {          // ~30 s of "typing", the most a free Worker may keep going
    await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/sendChatAction`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chat, action: "typing" }),
    }).catch(() => {});
    await new Promise((r) => setTimeout(r, 4500));
  }
}

async function wake(env, force = false) {
  const last = Number((await env.Q.get("last_wake")) || 0);
  const now = Date.now();
  if (!force && now - last < DEBOUNCE_MS) return;   // a run is already starting and will pick this up
  await env.Q.put("last_wake", String(now), { expirationTtl: 3600 });
  await fetch(`https://api.github.com/repos/${env.REPO}/actions/workflows/bot.yml/dispatches`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.DISPATCH_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "koloda-telegram-relay",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main", inputs: { task: "tick" } }),
  });
}
