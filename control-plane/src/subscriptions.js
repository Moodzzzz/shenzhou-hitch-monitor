import {
  addSecondsIso, appendCookies, audit, cookie, corsHeaders, createSignedToken,
  escapeHtml, hmacHex, html, HttpError, ipHash, json, normalizeEmail, normalizeRules,
  nowIso, randomToken, rateLimit, readJson, sha256Hex, safeEqual, verifySignedToken,
} from "./core.js";
import { enqueueEmail, processOutbox } from "./mail.js";

export function pageShell(title, body, status = 200) {
  return html(`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>${escapeHtml(title)} · 神州车价</title>
    <style>
      body{margin:0;background:#f2ead9;color:#231c14;font-family:"Songti SC","STSong",serif}
      main{max-width:680px;margin:0 auto;padding:8vh 22px 60px}
      .ticket{border:1px solid #b6a07b;background:#fffaf0;padding:28px 26px;position:relative}
      .eyebrow{font-family:monospace;font-size:10px;letter-spacing:3px;color:#c3511f}
      h1{font-size:30px;line-height:1.25;margin:10px 0}
      p{font-size:14px;line-height:1.8;color:#746651}
      button,.btn{display:inline-block;border:1px solid #231c14;background:#231c14;color:#f2ead9;
        padding:11px 17px;font:inherit;font-size:13px;text-decoration:none;cursor:pointer;margin:6px 6px 6px 0}
      .secondary{background:transparent;color:#231c14}
      .danger{border-color:#b23d37;background:transparent;color:#b23d37}
      pre{white-space:pre-wrap;background:#f2ead9;border:1px dashed #d8c7a7;padding:12px;font-size:11px}
      .stamp{position:absolute;right:20px;top:20px;border:2px solid #c3511f;color:#c3511f;
        font-family:monospace;font-size:9px;letter-spacing:2px;padding:6px;transform:rotate(5deg)}
      @media(max-width:520px){h1{font-size:24px}.ticket{padding:22px 18px}.stamp{display:none}}
    </style></head><body><main><section class="ticket">
    <div class="stamp">VERIFIED</div><div class="eyebrow">SZ CAR FARE / PRIVATE TICKET</div>
    <h1>${escapeHtml(title)}</h1>${body}</section></main></body></html>`, status);
}

function extractToken(request, body = {}) {
  const url = new URL(request.url);
  const token = body.token || url.searchParams.get("token") || "";
  if (!token || token.length > 500) throw new HttpError(400, "令牌缺失或无效");
  return token;
}

async function parseForm(request) {
  const type = request.headers.get("Content-Type") || "";
  if (type.includes("application/json")) return readJson(request, 32768);
  const text = await request.text();
  return Object.fromEntries(new URLSearchParams(text));
}

async function tokenRow(db, token, purpose) {
  return db.prepare(
    `SELECT t.id, t.identity_id AS identityId, t.subscription_id AS subscriptionId,
            t.expires_at AS expiresAt, t.consumed_at AS consumedAt,
            i.email, i.status AS identityStatus
     FROM auth_tokens t JOIN email_identities i ON i.id = t.identity_id
     WHERE t.token_hash = ? AND t.purpose = ? AND t.expires_at > ?`,
  ).bind(await sha256Hex(token), purpose, nowIso()).first();
}

function ruleSummary(rules) {
  const items = [];
  for (const key of ["pickup_cities", "return_cities", "any_cities", "routes", "brands", "models"]) {
    if (Array.isArray(rules[key]) && rules[key].length) items.push(`${key}: ${rules[key].join(" / ")}`);
  }
  for (const key of ["max_price", "max_price_per_day", "min_days", "max_days"]) {
    if (rules[key] != null) items.push(`${key}: ${rules[key]}`);
  }
  return items;
}

export async function createSubscription(request, env, ctx) {
  const cors = corsHeaders(env, request);
  const body = await readJson(request, 16384);
  const email = normalizeEmail(body.email);
  if (!email) throw new HttpError(400, "邮箱格式无效", "invalid_email");
  const rules = normalizeRules(body.rules);
  const ip = await ipHash(request);
  await rateLimit(env.DB, `sub:ip:${ip}`, 8, 3600);
  await rateLimit(env.DB, `sub:email:${await sha256Hex(email)}`, 4, 3600);

  const identity = await env.DB.prepare(
    `INSERT INTO email_identities(email, status, created_at, updated_at)
     VALUES(?, 'pending', ?, ?)
     ON CONFLICT(email) DO UPDATE SET updated_at = excluded.updated_at
     RETURNING id, status, verified_at AS verifiedAt`,
  ).bind(email, nowIso(), nowIso()).first();
  const alreadyVerified = identity?.status === "active" && Boolean(identity.verifiedAt);
  const status = alreadyVerified ? "active" : "pending";
  const subscription = await env.DB.prepare(
    `INSERT INTO subscriptions(identity_id, rules_json, status, created_at, updated_at, confirmed_at)
     VALUES(?, ?, ?, ?, ?, ?) RETURNING id`,
  ).bind(
    identity.id, JSON.stringify(rules), status, nowIso(), nowIso(),
    alreadyVerified ? nowIso() : null,
  ).first();

  if (alreadyVerified) {
    const manageToken = await createSignedToken(
      env.SESSION_SECRET, { sub: Number(subscription.id) }, 90 * 24 * 60 * 60,
    );
    await enqueueEmail(env.DB, {
      dedupeKey: `welcome:${subscription.id}:${Date.now()}`,
      kind: "system", template: "welcome", subscriptionId: subscription.id,
      identityId: identity.id, payload: { token: manageToken },
    });
  } else {
    const raw = randomToken(32);
    await env.DB.prepare(
      `INSERT INTO auth_tokens(token_hash, purpose, identity_id, subscription_id, created_at, expires_at)
       VALUES(?, 'verify', ?, ?, ?, ?)`,
    ).bind(await sha256Hex(raw), identity.id, subscription.id, nowIso(), addSecondsIso(86400)).run();
    await enqueueEmail(env.DB, {
      dedupeKey: `verify:${subscription.id}:${await sha256Hex(raw)}`,
      kind: "system", template: "verify", subscriptionId: subscription.id,
      identityId: identity.id, payload: { token: raw },
    });
  }
  await audit(env.DB, {
    actor: `email:${identity.id}`, action: "subscription.request", targetType: "subscription",
    targetId: subscription.id, detail: { verifiedEmail: alreadyVerified }, ip,
  });
  ctx.waitUntil(processOutbox(env, request, 5));
  return json({
    ok: true,
    message: alreadyVerified
      ? "已提交，请查收管理邮件。"
      : "已提交，请查收验证邮件；验证后才会发送提醒。",
  }, 202, cors);
}

export async function verifyGet(request, env) {
  const token = extractToken(request);
  const row = await tokenRow(env.DB, token, "verify");
  if (!row || row.consumedAt) throw new HttpError(410, "验证链接已使用或已过期");
  return pageShell(
    "确认这张提醒票",
    `<p>确认后，只有命中所选条件的车辆才会发信。</p>
     <p style="font-family:monospace;font-size:12px">${escapeHtml(row.email.replace(/(.).+(@.*)/, "$1***$2"))}</p>
     <form method="post" action="/api/subscriptions/verify">
       <input type="hidden" name="token" value="${escapeHtml(token)}">
       <button type="submit">确认订阅</button>
     </form>
     <p style="font-size:11px">此页面不会在加载时自动确认，避免邮件扫描器误触。</p>`,
  );
}

export async function verifyPost(request, env) {
  const body = await parseForm(request);
  const token = extractToken(request, body);
  const row = await tokenRow(env.DB, token, "verify");
  if (!row || row.consumedAt) throw new HttpError(410, "验证链接已使用或已过期");
  const consumed = await env.DB.prepare(
    "UPDATE auth_tokens SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
  ).bind(nowIso(), row.id).run();
  if (!Number(consumed.meta?.changes || 0)) throw new HttpError(409, "验证链接已被使用");
  await env.DB.batch([
    env.DB.prepare(
      "UPDATE email_identities SET status='active', verified_at=?, updated_at=? WHERE id=?",
    ).bind(nowIso(), nowIso(), row.identityId),
    env.DB.prepare(
      "UPDATE subscriptions SET status='active', confirmed_at=?, updated_at=? WHERE id=?",
    ).bind(nowIso(), nowIso(), row.subscriptionId),
  ]);
  const manageToken = await createSignedToken(
    env.SESSION_SECRET, { sub: Number(row.subscriptionId) }, 90 * 24 * 60 * 60,
  );
  await enqueueEmail(env.DB, {
    dedupeKey: `welcome:${row.subscriptionId}:${Date.now()}`,
    kind: "system", template: "welcome", subscriptionId: row.subscriptionId,
    identityId: row.identityId, payload: { token: manageToken },
  });
  await audit(env.DB, {
    actor: `email:${row.identityId}`, action: "subscription.verify",
    targetType: "subscription", targetId: row.subscriptionId, ip: await ipHash(request),
  });
  return pageShell(
    "订阅已验真",
    `<p>验证完成。我们已把管理链接发到你的邮箱；以后可暂停、修改或退订。</p>
     <p><a class="btn" href="/api/subscriptions/manage?token=${encodeURIComponent(manageToken)}">现在管理提醒</a></p>`,
  );
}

async function signedSubscription(env, token) {
  const payload = await verifySignedToken(env.SESSION_SECRET, token);
  if (!payload?.sub) throw new HttpError(401, "管理链接无效或已过期");
  const row = await env.DB.prepare(
    `SELECT s.id, s.rules_json AS rulesJson, s.status, s.created_at AS createdAt,
            s.confirmed_at AS confirmedAt, i.email
     FROM subscriptions s JOIN email_identities i ON i.id = s.identity_id
     WHERE s.id = ? AND i.status = 'active'`,
  ).bind(Number(payload.sub)).first();
  if (!row) throw new HttpError(404, "订阅不存在或邮箱已停用");
  return row;
}

export async function manageGet(request, env) {
  const url = new URL(request.url);
  const token = extractToken(request);
  const row = await signedSubscription(env, token);
  let rules = {};
  try { rules = JSON.parse(row.rulesJson); } catch { rules = {}; }
  const unsubscribe = url.searchParams.get("unsubscribe") === "1";
  const lines = ruleSummary(rules).map((line) => `<li>${escapeHtml(line)}</li>`).join("");
  const actionButtons = unsubscribe
    ? `<form method="post" action="/api/subscriptions/manage">
         <input type="hidden" name="token" value="${escapeHtml(token)}">
         <input type="hidden" name="action" value="unsubscribe">
         <button class="danger" type="submit">确认退订</button>
       </form>`
    : `<form method="post" action="/api/subscriptions/manage">
         <input type="hidden" name="token" value="${escapeHtml(token)}">
         <button class="${row.status === "active" ? "secondary" : ""}" name="action" value="resume">${row.status === "active" ? "已开启" : "恢复提醒"}</button>
         <button class="secondary" name="action" value="pause">暂停提醒</button>
         <button class="danger" name="action" value="unsubscribe">退订</button>
       </form>`;
  return pageShell(
    row.status === "canceled" ? "这张票已退订" : "管理我的提醒",
    `<p>订阅状态：<b>${escapeHtml(row.status)}</b> · ${escapeHtml(row.email)}</p>
     <ul style="line-height:1.9;color:#4b3f31;font-size:13px">${lines || "<li>未记录具体条件</li>"}</ul>
     ${row.status === "canceled" ? "<p>该订阅已停用，不会继续发送提醒。</p>" : actionButtons}`,
  );
}

export async function managePost(request, env) {
  const body = await parseForm(request);
  const token = extractToken(request, body);
  const row = await signedSubscription(env, token);
  const action = String(body.action || "");
  if (!["pause", "resume", "unsubscribe"].includes(action)) throw new HttpError(400, "未知操作");
  const status = action === "unsubscribe" ? "canceled" : action === "resume" ? "active" : "paused";
  await env.DB.prepare(
    "UPDATE subscriptions SET status=?, updated_at=? WHERE id=?",
  ).bind(status, nowIso(), row.id).run();
  await audit(env.DB, {
    actor: `email:${row.email}`, action: `subscription.${action}`,
    targetType: "subscription", targetId: row.id, ip: await ipHash(request),
  });
  return pageShell(
    status === "canceled" ? "已退订" : status === "paused" ? "提醒已暂停" : "提醒已恢复",
    `<p>当前状态：<b>${status}</b>。可以关闭本页。</p>
     <p><a class="btn secondary" href="/api/subscriptions/manage?token=${encodeURIComponent(token)}">返回管理页</a></p>`,
  );
}

export async function requireInternal(request, env) {
  const header = request.headers.get("Authorization") || "";
  const token = header.startsWith("Bearer ") ? header.slice(7) : "";
  if (!token || !env.INTERNAL_TOKEN || !(await safeEqual(token, env.INTERNAL_TOKEN))) {
    throw new HttpError(401, "内部令牌无效");
  }
}

export async function internalSubscriptions(request, env) {
  await requireInternal(request, env);
  const rows = await env.DB.prepare(
    `SELECT s.id, s.rules_json AS rulesJson, s.created_at AS createdAt
     FROM subscriptions s JOIN email_identities i ON i.id = s.identity_id
     WHERE s.status = 'active' AND i.status = 'active'
     ORDER BY s.id ASC LIMIT 1000`,
  ).all();
  const subscriptions = (rows.results || []).map((row) => {
    let rules = {};
    try { rules = JSON.parse(row.rulesJson); } catch { rules = {}; }
    return { id: row.id, rules, createdAt: row.createdAt };
  });
  return json({ ok: true, subscriptions });
}

export async function internalOutbox(request, env) {
  await requireInternal(request, env);
  const body = await readJson(request, 262144);
  const jobs = Array.isArray(body.jobs) ? body.jobs.slice(0, 200) : [];
  let accepted = 0;
  let skipped = 0;
  for (const job of jobs) {
    const subscriptionId = Number(job.subscriptionId || 0);
    const eventKey = String(job.eventKey || "").slice(0, 160);
    if (!subscriptionId || !eventKey) { skipped += 1; continue; }
    const row = await env.DB.prepare(
      `SELECT s.id, s.identity_id AS identityId
       FROM subscriptions s JOIN email_identities i ON i.id = s.identity_id
       WHERE s.id = ? AND s.status = 'active' AND i.status = 'active'`,
    ).bind(subscriptionId).first();
    if (!row) { skipped += 1; continue; }
    const inserted = await enqueueEmail(env.DB, {
      dedupeKey: `evt:${subscriptionId}:${eventKey}`,
      kind: "alert", template: "alert", subscriptionId, identityId: row.identityId,
      payload: job.payload && typeof job.payload === "object" ? job.payload : {},
    });
    if (!Number(inserted.meta?.changes || 0)) {
      skipped += 1;
      continue;
    }
    await env.DB.prepare("UPDATE subscriptions SET last_event_at=? WHERE id=?")
      .bind(nowIso(), subscriptionId).run();
    accepted += 1;
  }
  return json({ ok: true, accepted, skipped });
}

export function corsPreflight(request, env) {
  return new Response(null, { status: 204, headers: corsHeaders(env, request) });
}
