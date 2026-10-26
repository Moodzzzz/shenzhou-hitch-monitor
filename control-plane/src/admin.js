import {
  audit, escapeHtml, html, HttpError, ipHash, json, maskEmail, normalizeEmail,
  nowIso, readJson,
} from "./core.js";
import { requireAdmin } from "./auth.js";
import { renderTemplate, sendEmail } from "./mail.js";

function adminShell(session, env) {
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="csrf-token" content="${escapeHtml(session.csrfToken)}">
  <title>调度控制台 · 神州车价</title>
  <style>
    :root{--bg:#f2ead9;--paper:#fffaf0;--ink:#231c14;--muted:#746651;--line:#d8c7a7;--accent:#c3511f;--jade:#2f6c56;--red:#b23d37}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:"PingFang SC","Microsoft YaHei",sans-serif}
    header{display:flex;align-items:center;gap:20px;padding:18px 30px;border-bottom:1px solid #b6a07b;flex-wrap:wrap}
    .brand{font:700 19px "Songti SC",serif;letter-spacing:.05em}.brand small{display:block;font:9px monospace;letter-spacing:.22em;color:var(--accent)}
    nav{display:flex;gap:2px;flex-wrap:wrap}nav button{border:0;background:transparent;padding:9px 12px;font:12px "PingFang SC";color:var(--muted);cursor:pointer}
    nav button.on{background:var(--ink);color:var(--bg)}.who{margin-left:auto;font:11px monospace;color:var(--muted)}
    main{max-width:1280px;margin:0 auto;padding:26px 30px 70px}.view[hidden]{display:none}
    h1{font:700 30px "Songti SC",serif;margin:0 0 6px}h2{font:700 20px "Songti SC",serif;margin:0 0 4px}
    .sub{color:var(--muted);font-size:12px;line-height:1.8;margin:0 0 20px}
    .cards{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--line);border-bottom:1px solid var(--line);margin-bottom:22px}
    .card{padding:16px 18px;border-left:1px solid var(--line)}.card:first-child{border-left:0}
    .card b{display:block;font:600 28px "Bahnschrift",monospace}.card span{font-size:10px;letter-spacing:.12em;color:var(--muted)}
    .toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:12px 0}
    button.action{border:1px solid var(--ink);background:var(--ink);color:var(--bg);padding:9px 13px;cursor:pointer;font:12px inherit}
    button.ghost{background:transparent;color:var(--ink);border-color:var(--line)}button.danger{color:var(--red);border-color:var(--red);background:transparent}
    input,select{border:1px solid var(--line);background:var(--paper);padding:9px 10px;font:12px inherit;color:var(--ink)}
    .paper{border:1px solid var(--line);background:var(--paper);padding:18px;overflow:auto}
    table{width:100%;border-collapse:collapse;font-size:11.5px}th{font:10px monospace;letter-spacing:.1em;text-align:left;color:var(--muted);padding:8px 7px;border-bottom:1px solid #b6a07b}
    td{padding:9px 7px;border-bottom:1px dashed var(--line);vertical-align:top}td.mono{font-family:monospace;white-space:nowrap}
    .st{display:inline-block;border:1px solid currentColor;padding:2px 5px;font:9px monospace;white-space:nowrap}
    .st.active,.st.sent{color:var(--jade)}.st.pending,.st.queued{color:var(--accent)}.st.paused,.st.dead,.st.canceled{color:var(--red)}
    .empty{padding:40px;text-align:center;color:var(--muted);font-size:12px}
    .notice{font-size:11px;line-height:1.7;color:var(--muted);border-left:2px solid var(--accent);padding-left:10px;margin:12px 0}
    .toast{position:fixed;right:22px;bottom:22px;background:var(--ink);color:var(--bg);padding:10px 14px;font-size:12px;opacity:0;transition:.2s;pointer-events:none}
    .toast.show{opacity:1}
    @media(max-width:900px){.cards{grid-template-columns:1fr 1fr}header{padding:14px 16px}main{padding:20px 14px 60px}.who{width:100%;margin-left:0}}
  </style></head><body>
  <header><div class="brand">调度控制台<small>SZ CAR FARE / PRIVATE CONTROL PLANE</small></div>
    <nav><button data-view="overview" class="on">总览</button><button data-view="subscriptions">订阅</button>
      <button data-view="outbox">发信队列</button><button data-view="audit">审计</button><button data-view="settings">设置</button></nav>
    <div class="who">${escapeHtml(session.login)} · <button id="logout" style="border:0;background:none;color:inherit;cursor:pointer;font:inherit">退出</button></div>
  </header>
  <main>
    <section class="view" id="view-overview"><h1>运行总览</h1><p class="sub">公开数据继续由 GitHub Actions 生成；这里只管理私有订阅、发信和审计，不持有公开面板写权限。</p><div class="cards" id="overviewCards"></div><div class="paper" id="overviewHealth"></div></section>
    <section class="view" id="view-subscriptions" hidden><h1>私有订阅</h1><p class="sub">邮箱默认掩码；只有完成 double opt-in 的订阅才会接收提醒。</p><div class="toolbar"><select id="subStatus"><option value="">全部状态</option><option value="active">active</option><option value="pending">pending</option><option value="paused">paused</option><option value="canceled">canceled</option></select><button class="action" id="loadSubs">刷新</button></div><div class="paper" id="subTable"></div></section>
    <section class="view" id="view-outbox" hidden><h1>发信队列</h1><p class="sub">provider 已接受和最终送达分开记录；失败会退避重试，dead 可人工重试。</p><div class="toolbar"><select id="outboxStatus"><option value="">全部状态</option><option value="queued">queued</option><option value="sending">sending</option><option value="sent">sent</option><option value="dead">dead</option></select><button class="action" id="loadOutbox">刷新</button></div><div class="paper" id="outboxTable"></div></section>
    <section class="view" id="view-audit" hidden><h1>审计</h1><p class="sub">登录、订阅变更、后台重试与测试信都会记录；不保存邮件正文。</p><div class="paper" id="auditTable"></div></section>
    <section class="view" id="view-settings" hidden><h1>设置</h1><p class="sub">密钥只在 Cloudflare Secrets 中；这里只显示配置状态。</p><div class="paper" id="settingsBox"></div><div class="paper" style="margin-top:14px"><h2>测试投递</h2><p class="sub">只允许管理员触发。真实发信需要先配置 provider 与发信域。</p><div class="toolbar"><input id="testEmail" type="email" placeholder="收件邮箱"><button class="action" id="sendTest">发送测试信</button></div></div></section>
  </main><div class="toast" id="toast"></div>
  <script>
    const csrf=document.querySelector('meta[name="csrf-token"]').content;
    const $=s=>document.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const fmt=s=>s?String(s).replace('T',' ').slice(0,19):'—';
    async function api(path,opts={}){const r=await fetch(path,{...opts,headers:{...(opts.headers||{}),'X-CSRF-Token':csrf}});if(r.status===401){location.href='/admin';throw new Error('login');}const j=await r.json().catch(()=>({error:'bad json'}));if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j;}
    function toast(t){const el=$('#toast');el.textContent=t;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),1800)}
    function show(name){document.querySelectorAll('.view').forEach(v=>v.hidden=v.id!=='view-'+name);document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.view===name));if(name==='overview')loadOverview();if(name==='subscriptions')loadSubs();if(name==='outbox')loadOutbox();if(name==='audit')loadAudit();if(name==='settings')loadSettings();}
    async function loadOverview(){try{const d=await api('/api/admin/overview');$('#overviewCards').innerHTML=[['已验证订阅',d.subscriptions.active,'active'],['待验证',d.subscriptions.pending,'pending'],['队列待发',d.outbox.queued,'queued'],['发信失败',d.outbox.dead,'dead']].map(([k,v])=>'<div class="card"><b>'+v+'</b><span>'+k+'</span></div>').join('');$('#overviewHealth').innerHTML='<table><tr><th>项目</th><th>状态</th><th>说明</th></tr><tr><td>邮件 provider</td><td><span class="st '+(d.settings.emailConfigured?'active':'dead')+'">'+(d.settings.emailConfigured?'已配置':'未配置/控制台模式')+'</span></td><td class="mono">'+esc(d.settings.emailMode)+'</td></tr><tr><td>内部 ingest</td><td><span class="st '+(d.settings.internalConfigured?'active':'dead')+'">'+(d.settings.internalConfigured?'已配置':'缺失')+'</span></td><td>Actions 仅能读取规则并写通知任务</td></tr><tr><td>支付</td><td><span class="st">已预留未启用</span></td><td>不会创建真实收款或支付外呼</td></tr><tr><td>最近审计</td><td colspan="2">'+esc(fmt(d.latestAuditAt))+'</td></tr></table>';}catch(e){toast(e.message)}}
    async function loadSubs(){try{const s=$('#subStatus').value;const d=await api('/api/admin/subscriptions?status='+encodeURIComponent(s));$('#subTable').innerHTML=d.subscriptions.length?'<table><tr><th>ID</th><th>邮箱</th><th>规则</th><th>状态</th><th>确认时间</th><th>操作</th></tr>'+d.subscriptions.map(x=>'<tr><td class="mono">'+x.id+'</td><td>'+esc(x.email)+'</td><td>'+esc(x.name)+'</td><td><span class="st '+esc(x.status)+'">'+esc(x.status)+'</span></td><td class="mono">'+fmt(x.confirmedAt)+'</td><td><button class="action ghost" data-sub="'+x.id+'" data-status="'+(x.status==='active'?'paused':'active')+'">'+(x.status==='active'?'暂停':'恢复')+'</button> <button class="danger" data-sub="'+x.id+'" data-status="canceled">停用</button></td></tr>').join('')+'</table>':'<div class="empty">没有符合条件的订阅</div>';}catch(e){toast(e.message)}}
    async function loadOutbox(){try{const s=$('#outboxStatus').value;const d=await api('/api/admin/outbox?status='+encodeURIComponent(s));$('#outboxTable').innerHTML=d.emails.length?'<table><tr><th>ID</th><th>收件</th><th>模板</th><th>状态</th><th>尝试</th><th>下次</th><th>错误</th><th></th></tr>'+d.emails.map(x=>'<tr><td class="mono">'+x.id+'</td><td>'+esc(x.email)+'</td><td>'+esc(x.template)+'</td><td><span class="st '+esc(x.status)+'">'+esc(x.status)+'</span></td><td>'+x.attempts+'</td><td class="mono">'+fmt(x.nextAttemptAt)+'</td><td>'+esc(x.lastError||'')+'</td><td>'+(x.status==='dead'||x.attempts>0?'<button class="action ghost" data-retry="'+x.id+'">重试</button>':'')+'</td></tr>').join('')+'</table>':'<div class="empty">队列为空</div>';}catch(e){toast(e.message)}}
    async function loadAudit(){try{const d=await api('/api/admin/audit');$('#auditTable').innerHTML=d.logs.length?'<table><tr><th>时间</th><th>操作者</th><th>动作</th><th>对象</th></tr>'+d.logs.map(x=>'<tr><td class="mono">'+fmt(x.createdAt)+'</td><td>'+esc(x.actor)+'</td><td>'+esc(x.action)+'</td><td>'+esc(x.targetType)+' '+esc(x.targetId)+'</td></tr>').join('')+'</table>':'<div class="empty">暂无审计记录</div>';}catch(e){toast(e.message)}}
    async function loadSettings(){try{const d=await api('/api/admin/settings');$('#settingsBox').innerHTML='<table><tr><th>配置</th><th>值</th></tr>'+Object.entries(d.settings).map(([k,v])=>'<tr><td class="mono">'+esc(k)+'</td><td>'+esc(String(v))+'</td></tr>').join('')+'</table><p class="notice">这里只显示非秘密状态。API key、OAuth secret、内部令牌和未来支付密钥都不会返回到浏览器。</p>';}catch(e){toast(e.message)}}
    document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>show(b.dataset.view));
    $('#logout').onclick=async()=>{await api('/api/admin/logout',{method:'POST'});location.href='/admin'};
    $('#loadSubs').onclick=loadSubs;$('#subStatus').onchange=loadSubs;$('#loadOutbox').onclick=loadOutbox;$('#outboxStatus').onchange=loadOutbox;
    $('#subTable').onclick=async e=>{const b=e.target.closest('[data-sub]');if(!b)return;try{await api('/api/admin/subscriptions/'+b.dataset.sub+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:b.dataset.status})});toast('已更新');loadSubs();}catch(err){toast(err.message)}};
    $('#outboxTable').onclick=async e=>{const b=e.target.closest('[data-retry]');if(!b)return;try{await api('/api/admin/outbox/'+b.dataset.retry+'/retry',{method:'POST'});toast('已重新排队');loadOutbox();}catch(err){toast(err.message)}};
    $('#sendTest').onclick=async()=>{try{const email=$('#testEmail').value;await api('/api/admin/test-mail',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})});toast('测试信已入队');}catch(e){toast(e.message)}};
    loadOverview();
  </script></body></html>`;
}

export async function adminPage(request, env, session) {
  return html(adminShell(session, env));
}

export async function adminOverview(request, env) {
  await requireAdmin(request, env);
  const counts = await env.DB.batch([
    env.DB.prepare("SELECT status, COUNT(*) AS n FROM subscriptions GROUP BY status"),
    env.DB.prepare("SELECT status, COUNT(*) AS n FROM email_outbox GROUP BY status"),
    env.DB.prepare("SELECT MAX(created_at) AS latest FROM audit_logs"),
  ]);
  const subscriptions = { active: 0, pending: 0, paused: 0, canceled: 0 };
  for (const row of counts[0].results || []) subscriptions[row.status] = Number(row.n || 0);
  const outbox = { queued: 0, sending: 0, sent: 0, dead: 0 };
  for (const row of counts[1].results || []) outbox[row.status] = Number(row.n || 0);
  return json({
    ok: true,
    subscriptions,
    outbox,
    latestAuditAt: counts[2].results?.[0]?.latest || "",
    settings: {
      emailConfigured: Boolean(env.RESEND_API_KEY || env.EMAIL_MODE === "console"),
      emailMode: env.EMAIL_MODE || "resend",
      internalConfigured: Boolean(env.INTERNAL_TOKEN),
      paymentsEnabled: false,
    },
  });
}

export async function adminSubscriptions(request, env) {
  await requireAdmin(request, env);
  const url = new URL(request.url);
  const status = url.searchParams.get("status") || "";
  const allowed = new Set(["active", "pending", "paused", "canceled"]);
  const rows = status && allowed.has(status)
    ? await env.DB.prepare(
      `SELECT s.id, s.status, s.rules_json AS rulesJson, s.created_at AS createdAt,
              s.confirmed_at AS confirmedAt, i.email
       FROM subscriptions s JOIN email_identities i ON i.id = s.identity_id
       WHERE s.status = ? ORDER BY s.id DESC LIMIT 200`,
    ).bind(status).all()
    : await env.DB.prepare(
      `SELECT s.id, s.status, s.rules_json AS rulesJson, s.created_at AS createdAt,
              s.confirmed_at AS confirmedAt, i.email
       FROM subscriptions s JOIN email_identities i ON i.id = s.identity_id
       ORDER BY s.id DESC LIMIT 200`,
    ).all();
  const subscriptions = (rows.results || []).map((row) => {
    let rules = {};
    try { rules = JSON.parse(row.rulesJson); } catch { rules = {}; }
    return {
      id: row.id, email: maskEmail(row.email), status: row.status,
      name: rules.name || "我的订阅", createdAt: row.createdAt, confirmedAt: row.confirmedAt,
    };
  });
  return json({ ok: true, subscriptions });
}

export async function adminSubscriptionStatus(request, env, id) {
  const session = await requireAdmin(request, env);
  const body = await readJson(request, 4096);
  const status = String(body.status || "");
  if (!["active", "paused", "canceled"].includes(status)) throw new HttpError(400, "非法订阅状态");
  const row = await env.DB.prepare(
    `SELECT s.id, i.status AS identityStatus FROM subscriptions s
     JOIN email_identities i ON i.id = s.identity_id WHERE s.id = ?`,
  ).bind(Number(id)).first();
  if (!row) throw new HttpError(404, "订阅不存在");
  if (status === "active" && row.identityStatus !== "active") throw new HttpError(409, "邮箱尚未验证");
  await env.DB.prepare("UPDATE subscriptions SET status=?, updated_at=? WHERE id=?")
    .bind(status, nowIso(), row.id).run();
  await audit(env.DB, {
    actor: session.login, action: `admin.subscription.${status}`,
    targetType: "subscription", targetId: row.id, ip: await ipHash(request),
  });
  return json({ ok: true });
}

export async function adminOutbox(request, env) {
  await requireAdmin(request, env);
  const url = new URL(request.url);
  const status = url.searchParams.get("status") || "";
  const rows = status
    ? await env.DB.prepare(
      `SELECT o.id, o.template, o.status, o.attempts, o.next_attempt_at AS nextAttemptAt,
              o.last_error AS lastError, i.email
       FROM email_outbox o JOIN email_identities i ON i.id = o.identity_id
       WHERE o.status = ? ORDER BY o.id DESC LIMIT 200`,
    ).bind(status).all()
    : await env.DB.prepare(
      `SELECT o.id, o.template, o.status, o.attempts, o.next_attempt_at AS nextAttemptAt,
              o.last_error AS lastError, i.email
       FROM email_outbox o JOIN email_identities i ON i.id = o.identity_id
       ORDER BY o.id DESC LIMIT 200`,
    ).all();
  return json({
    ok: true,
    emails: (rows.results || []).map((row) => ({ ...row, email: maskEmail(row.email) })),
  });
}

export async function adminOutboxRetry(request, env, id) {
  const session = await requireAdmin(request, env);
  const row = await env.DB.prepare("SELECT id FROM email_outbox WHERE id = ?").bind(Number(id)).first();
  if (!row) throw new HttpError(404, "队列项不存在");
  await env.DB.prepare(
    `UPDATE email_outbox SET status='queued', attempts=0, next_attempt_at=?, lease_until=NULL, last_error=NULL
     WHERE id=?`,
  ).bind(nowIso(), row.id).run();
  await audit(env.DB, {
    actor: session.login, action: "admin.outbox.retry", targetType: "outbox",
    targetId: row.id, ip: await ipHash(request),
  });
  return json({ ok: true });
}

export async function adminAudit(request, env) {
  await requireAdmin(request, env);
  const rows = await env.DB.prepare(
    `SELECT actor, action, target_type AS targetType, target_id AS targetId, created_at AS createdAt
     FROM audit_logs ORDER BY id DESC LIMIT 200`,
  ).all();
  return json({ ok: true, logs: rows.results || [] });
}

export async function adminSettings(request, env) {
  await requireAdmin(request, env);
  return json({
    ok: true,
    settings: {
      PUBLIC_WORKER_ORIGIN: env.PUBLIC_WORKER_ORIGIN || "",
      PUBLIC_DASHBOARD_ORIGIN: env.PUBLIC_DASHBOARD_ORIGIN || "",
      ADMIN_GITHUB_LOGINS: env.ADMIN_GITHUB_LOGINS || "",
      EMAIL_PROVIDER: env.EMAIL_PROVIDER || "resend",
      EMAIL_MODE: env.EMAIL_MODE || "resend",
      EMAIL_FROM: env.EMAIL_FROM || "",
      emailApiConfigured: Boolean(env.RESEND_API_KEY),
      internalConfigured: Boolean(env.INTERNAL_TOKEN),
      paymentsEnabled: false,
      paymentProvidersReserved: "wechat_alipay",
      schedule: "outbox every 2 minutes",
    },
  });
}

export async function adminTestMail(request, env) {
  const session = await requireAdmin(request, env);
  const body = await readJson(request, 4096);
  const email = normalizeEmail(body.email);
  if (!email) throw new HttpError(400, "邮箱格式无效");
  const message = renderTemplate("test", {}, env, request);
  const result = await sendEmail(env, {
    ...message, to: email, idempotencyKey: `test-${crypto.randomUUID()}`,
  });
  await audit(env.DB, {
    actor: session.login, action: "admin.test_mail", targetType: "email",
    targetId: maskEmail(email), detail: { provider: result.provider }, ip: await ipHash(request),
  });
  return json({ ok: true, provider: result.provider, accepted: true });
}
