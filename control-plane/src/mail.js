import {
  addSecondsIso, createSignedToken, escapeHtml, maskEmail, nowIso, retryMinutes,
} from "./core.js";

const MAX_ATTEMPTS = 5;

export function publicBase(env, request) {
  return String(env.PUBLIC_WORKER_ORIGIN || env.PUBLIC_CONTROL_PLANE_URL || new URL(request.url).origin)
    .replace(/\/$/, "");
}

export function emailShell(title, intro, body) {
  return `<!doctype html><html lang="zh-CN"><body style="margin:0;background:#f2ead9;color:#231c14;font-family:Georgia,'Songti SC',serif">
  <div style="max-width:620px;margin:0 auto;padding:28px 18px">
    <div style="border:1px solid #b6a07b;background:#fffaf0;padding:26px 24px">
      <div style="font-family:monospace;font-size:10px;letter-spacing:3px;color:#c3511f">SZ CAR FARE / DISPATCH TICKET</div>
      <h1 style="font-size:25px;line-height:1.3;margin:12px 0 8px">${escapeHtml(title)}</h1>
      <p style="color:#746651;font-size:13px;line-height:1.7;margin:0 0 20px">${escapeHtml(intro)}</p>
      ${body}
      <hr style="border:0;border-top:1px dashed #d8c7a7;margin:24px 0 14px">
      <p style="font-family:monospace;font-size:10px;line-height:1.7;color:#796c58;margin:0">
        神州车价 · 价格与租期以官方 App 订单页为准。此邮件由你订阅的提醒规则触发。
      </p>
    </div>
  </div></body></html>`;
}

export function renderTemplate(template, payload, env, request) {
  const base = publicBase(env, request);
  if (template === "verify") {
    const link = `${base}/api/subscriptions/verify?token=${encodeURIComponent(payload.token || "")}`;
    return {
      subject: "确认你的神州车价提醒",
      html: emailShell(
        "还差一步，验真这张提醒票",
        "确认邮箱后，只有命中所选城市、路线或车型时才发信。",
        `<a href="${escapeHtml(link)}" style="display:inline-block;background:#231c14;color:#f2ead9;padding:12px 18px;text-decoration:none">确认订阅</a>
         <p style="font-size:11px;color:#796c58;line-height:1.7;margin-top:16px">链接 24 小时内有效。若不是你发起，请忽略本邮件。</p>`,
      ),
      text: `确认神州车价提醒：${link}\n链接 24 小时内有效。`,
    };
  }
  if (template === "welcome") {
    const link = `${base}/api/subscriptions/manage?token=${encodeURIComponent(payload.token || "")}`;
    return {
      subject: "订阅已生效 · 神州车价",
      html: emailShell(
        "订阅已验真",
        "以后命中规则才会发信；可以随时暂停、改规则或退订。",
        `<a href="${escapeHtml(link)}" style="display:inline-block;border:1px solid #b6a07b;color:#231c14;padding:11px 16px;text-decoration:none">管理我的提醒</a>`,
      ),
      text: `订阅已生效。管理链接：${link}`,
    };
  }
  if (template === "test") {
    return {
      subject: "测试信 · 神州车价后台",
      html: emailShell(
        "发信链路已连通",
        "这是一封管理员主动触发的测试邮件，不代表有新车命中。",
        `<p style="font-size:13px">发送时间：${escapeHtml(nowIso())}</p>`,
      ),
      text: `测试信已连通。发送时间：${nowIso()}`,
    };
  }
  const events = Array.isArray(payload.events) ? payload.events.slice(0, 30) : [];
  const cards = events.map((event) => {
    const title = escapeHtml(event.title || event.name || "车辆提醒");
    const route = escapeHtml(event.route || "");
    const price = event.price == null ? "" : `¥${escapeHtml(event.price)}`;
    const when = escapeHtml(event.when || "");
    return `<div style="border-top:1px dashed #d8c7a7;padding:12px 0">
      <div style="font-size:15px;font-weight:bold">${title}</div>
      <div style="font-size:12px;color:#746651;line-height:1.7;margin-top:4px">${route}${when ? " · " + when : ""}</div>
      <div style="font-family:monospace;font-size:18px;color:#c3511f;margin-top:5px">${price}</div>
    </div>`;
  }).join("") || `<p style="font-size:12px;color:#746651">本轮命中事件已整理，但详情为空。</p>`;
  const manage = payload.manageUrl
    ? `<p style="font-size:11px;color:#796c58;line-height:1.7;margin-top:16px"><a href="${escapeHtml(payload.manageUrl)}" style="color:#c3511f">管理或退订这条提醒</a></p>`
    : "";
  return {
    subject: payload.subject || `神州车价 · ${events.length} 条新提醒`,
    html: emailShell("你的条件有动静", payload.intro || "以下只包含当前订阅命中的车辆。", cards + manage),
    text: `${events.map((event) => `${event.title || "车辆"} ${event.route || ""} ${event.price ?? ""}`).join("\n")}${payload.manageUrl ? `\n管理/退订：${payload.manageUrl}` : ""}`,
    headers: payload.unsubscribeUrl ? { "List-Unsubscribe": `<${payload.unsubscribeUrl}>` } : undefined,
  };
}

export async function sendEmail(env, message) {
  if ((env.EMAIL_MODE || "resend") === "console") {
    console.log("email_console", {
      to: maskEmail(message.to),
      subject: message.subject,
      idempotencyKey: message.idempotencyKey,
    });
    return { provider: "console", id: `console-${crypto.randomUUID()}` };
  }
  if (!env.RESEND_API_KEY || !env.EMAIL_FROM) throw new Error("邮件 provider 未配置");
  const response = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.RESEND_API_KEY}`,
      "Content-Type": "application/json",
      "Idempotency-Key": message.idempotencyKey,
    },
    body: JSON.stringify({
      from: env.EMAIL_FROM,
      to: [message.to],
      subject: message.subject,
      html: message.html,
      text: message.text,
      headers: message.headers || undefined,
    }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.id) {
    throw new Error(`Resend ${response.status}: ${String(data.message || data.name || "发送失败").slice(0, 240)}`);
  }
  return { provider: "resend", id: data.id };
}

export async function enqueueEmail(db, {
  dedupeKey, kind, template, subscriptionId = null, identityId, payload = {},
  delaySeconds = 0,
}) {
  return db.prepare(
    `INSERT INTO email_outbox(
       dedupe_key, kind, template, subscription_id, identity_id, payload_json,
       status, attempts, next_attempt_at, created_at
     ) VALUES(?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
     ON CONFLICT(dedupe_key) DO NOTHING`,
  ).bind(
    dedupeKey, kind, template, subscriptionId, identityId,
    JSON.stringify(payload), addSecondsIso(delaySeconds), nowIso(),
  ).run();
}

export async function claimOutbox(db, limit = 20) {
  const now = nowIso();
  const lease = addSecondsIso(120);
  const rows = await db.prepare(
    `UPDATE email_outbox
     SET status = 'sending', lease_until = ?
     WHERE id IN (
       SELECT id FROM email_outbox
       WHERE (status = 'queued' AND next_attempt_at <= ?)
          OR (status = 'sending' AND lease_until < ?)
       ORDER BY next_attempt_at ASC LIMIT ?
     )
     RETURNING *`,
  ).bind(lease, now, now, limit).all();
  return rows.results || [];
}

export async function processOutbox(env, request, limit = 20) {
  const rows = await claimOutbox(env.DB, limit);
  let sent = 0;
  let failed = 0;
  for (const row of rows) {
    try {
      const identity = await env.DB.prepare(
        "SELECT email, status FROM email_identities WHERE id = ?",
      ).bind(row.identity_id).first();
      if (!identity?.email) throw new Error("收件人不存在");
      if (row.template !== "verify" && identity.status !== "active") {
        throw new Error("收件人未验证或已停用");
      }
      let payload = {};
      try { payload = JSON.parse(row.payload_json || "{}"); } catch { payload = {}; }
      if (row.template === "alert" && row.subscription_id && env.SESSION_SECRET) {
        const token = await createSignedToken(
          env.SESSION_SECRET, { sub: Number(row.subscription_id) }, 90 * 24 * 60 * 60,
        );
        const base = publicBase(env, request);
        payload.manageUrl = `${base}/api/subscriptions/manage?token=${encodeURIComponent(token)}`;
        payload.unsubscribeUrl = `${base}/api/subscriptions/manage?token=${encodeURIComponent(token)}&unsubscribe=1`;
      }
      const message = renderTemplate(row.template, payload, env, request);
      const result = await sendEmail(env, {
        ...message,
        to: identity.email,
        idempotencyKey: `outbox-${row.id}`,
      });
      await env.DB.batch([
        env.DB.prepare(
          `UPDATE email_outbox SET status='sent', provider_message_id=?, sent_at=?,
             lease_until=NULL, payload_json='{}', last_error=NULL WHERE id=?`,
        ).bind(result.id, nowIso(), row.id),
        env.DB.prepare(
          `INSERT INTO deliveries(outbox_id, provider, provider_message_id, status, created_at)
           VALUES(?, ?, ?, 'sent', ?)`,
        ).bind(row.id, result.provider, result.id, nowIso()),
      ]);
      sent += 1;
    } catch (error) {
      const attempts = Number(row.attempts || 0) + 1;
      const dead = attempts >= MAX_ATTEMPTS;
      const message = String(error?.message || error).slice(0, 500);
      await env.DB.batch([
        env.DB.prepare(
          `UPDATE email_outbox SET status=?, attempts=?, next_attempt_at=?,
             lease_until=NULL, last_error=? WHERE id=?`,
        ).bind(
          dead ? "dead" : "queued", attempts,
          addSecondsIso(retryMinutes(attempts) * 60), message, row.id,
        ),
        env.DB.prepare(
          `INSERT INTO deliveries(outbox_id, provider, status, error, created_at)
           VALUES(?, ?, ?, ?, ?)`,
        ).bind(row.id, env.EMAIL_PROVIDER || "resend", dead ? "dead" : "failed", message, nowIso()),
      ]);
      failed += 1;
    }
  }
  return { claimed: rows.length, sent, failed };
}
