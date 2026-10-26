import {
  addSecondsIso, appendCookies, audit, clearCookie, cookie, hmacHex, html, HttpError,
  ipHash, json, nowIso, parseCookies, randomToken, redirect, safeEqual, sha256Hex,
} from "./core.js";

const SESSION_COOKIE = "sz_admin";
const OAUTH_COOKIE = "sz_oauth";
const SESSION_SECONDS = 12 * 60 * 60;

function base64url(bytes) {
  let binary = "";
  for (const value of bytes) binary += String.fromCharCode(value);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

async function pkceChallenge(verifier) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

async function signPayload(secret, payload) {
  const value = btoa(JSON.stringify(payload)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  return `${value}.${await hmacHex(secret, value)}`;
}

async function verifyPayload(secret, signed) {
  const [value, signature] = String(signed || "").split(".");
  if (!value || !signature) return null;
  if (!(await safeEqual(signature, await hmacHex(secret, value)))) return null;
  try {
    const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=")));
  } catch {
    return null;
  }
}

function secureCookies(request) {
  return new URL(request.url).protocol === "https:";
}

export async function startGithubLogin(request, env) {
  if (!env.GITHUB_APP_CLIENT_ID || !env.GITHUB_APP_CLIENT_SECRET || !env.SESSION_SECRET) {
    throw new HttpError(503, "管理员 OAuth 尚未配置");
  }
  const state = randomToken(24);
  const verifier = randomToken(48);
  const oauth = await signPayload(env.SESSION_SECRET, {
    state, verifier, exp: Date.now() + 10 * 60 * 1000,
  });
  const callback = `${String(env.PUBLIC_WORKER_ORIGIN || new URL(request.url).origin).replace(/\/$/, "")}/admin/auth/github/callback`;
  const target = new URL("https://github.com/login/oauth/authorize");
  target.searchParams.set("client_id", env.GITHUB_APP_CLIENT_ID);
  target.searchParams.set("redirect_uri", callback);
  target.searchParams.set("state", state);
  target.searchParams.set("code_challenge", await pkceChallenge(verifier));
  target.searchParams.set("code_challenge_method", "S256");
  return redirect(target.toString(), {
    "Set-Cookie": cookie(OAUTH_COOKIE, oauth, {
      maxAge: 600, secure: secureCookies(request), sameSite: "Lax",
    }),
  });
}

async function exchangeCode(env, code, verifier, redirectUri) {
  const body = new URLSearchParams({
    client_id: env.GITHUB_APP_CLIENT_ID,
    client_secret: env.GITHUB_APP_CLIENT_SECRET,
    code,
    redirect_uri: redirectUri,
    code_verifier: verifier,
  });
  const response = await fetch("https://github.com/login/oauth/access_token", {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!response.ok) throw new HttpError(502, "GitHub 登录交换失败");
  const data = await response.json();
  if (!data.access_token) throw new HttpError(401, "GitHub 未返回访问令牌");
  return data.access_token;
}

export async function githubCallback(request, env) {
  const url = new URL(request.url);
  const code = url.searchParams.get("code") || "";
  const state = url.searchParams.get("state") || "";
  const cookies = parseCookies(request);
  const oauth = await verifyPayload(env.SESSION_SECRET, cookies.get(OAUTH_COOKIE));
  if (!code || !oauth || oauth.exp < Date.now() || !(await safeEqual(state, oauth.state))) {
    throw new HttpError(400, "登录状态无效或已过期");
  }
  const redirectUri = `${String(env.PUBLIC_WORKER_ORIGIN || url.origin).replace(/\/$/, "")}/admin/auth/github/callback`;
  const accessToken = await exchangeCode(env, code, oauth.verifier, redirectUri);
  const userResponse = await fetch("https://api.github.com/user", {
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${accessToken}`,
      "User-Agent": "szhitch-control-plane",
      "X-GitHub-Api-Version": "2022-11-28",
    },
  });
  if (!userResponse.ok) throw new HttpError(502, "读取 GitHub 账户失败");
  const github = await userResponse.json();
  const login = String(github.login || "");
  const allowed = String(env.ADMIN_GITHUB_LOGINS || "")
    .split(",").map((item) => item.trim()).filter(Boolean);
  if (!login || !allowed.includes(login)) {
    throw new HttpError(403, "该 GitHub 账户没有管理员权限");
  }
  const admin = await env.DB.prepare(
    `INSERT INTO admin_users(github_id, github_login, enabled, created_at)
     VALUES(?, ?, 1, ?)
     ON CONFLICT(github_login) DO UPDATE SET github_id = excluded.github_id, last_login_at = ?
     RETURNING id`,
  ).bind(String(github.id), login, nowIso(), nowIso()).first();
  if (!admin?.id) throw new HttpError(500, "管理员账户写入失败");
  const token = randomToken(32);
  const csrf = randomToken(24);
  await env.DB.prepare(
    `INSERT INTO sessions(token_hash, admin_id, csrf_token, created_at, expires_at, last_seen_at, ip_hash, user_agent)
     VALUES(?, ?, ?, ?, ?, ?, ?, ?)`,
  ).bind(
    await sha256Hex(token), admin.id, csrf, nowIso(), addSecondsIso(SESSION_SECONDS),
    nowIso(), await ipHash(request), (request.headers.get("User-Agent") || "").slice(0, 300),
  ).run();
  await audit(env.DB, {
    actor: login, action: "admin.login", targetType: "admin", targetId: admin.id,
    ip: await ipHash(request),
  });
  return appendCookies(redirect("/admin"), [
    cookie(SESSION_COOKIE, token, {
      maxAge: SESSION_SECONDS, secure: secureCookies(request), sameSite: "Lax",
    }),
    clearCookie(OAUTH_COOKIE, { secure: secureCookies(request), sameSite: "Lax" }),
  ]);
}

export async function getAdminSession(request, env) {
  const token = parseCookies(request).get(SESSION_COOKIE);
  if (!token) return null;
  const row = await env.DB.prepare(
    `SELECT s.id, s.admin_id AS adminId, s.csrf_token AS csrfToken, s.expires_at AS expiresAt,
            a.github_login AS login, a.enabled
     FROM sessions s JOIN admin_users a ON a.id = s.admin_id
     WHERE s.token_hash = ? AND s.expires_at > ? AND a.enabled = 1`,
  ).bind(await sha256Hex(token), nowIso()).first();
  if (!row) return null;
  await env.DB.prepare("UPDATE sessions SET last_seen_at = ? WHERE id = ?")
    .bind(nowIso(), row.id).run();
  return row;
}

export async function requireAdmin(request, env) {
  const session = await getAdminSession(request, env);
  if (!session) throw new HttpError(401, "需要管理员登录", "admin_required");
  if (!["GET", "HEAD"].includes(request.method.toUpperCase())) assertSameOrigin(request, env);
  const csrf = request.headers.get("X-CSRF-Token") || "";
  if (!(await safeEqual(csrf, session.csrfToken))) {
    throw new HttpError(403, "CSRF 校验失败", "csrf_failed");
  }
  return session;
}

export function assertSameOrigin(request, env) {
  const origin = request.headers.get("Origin") || "";
  const expected = String(env.PUBLIC_WORKER_ORIGIN || new URL(request.url).origin).replace(/\/$/, "");
  if (origin !== expected) throw new HttpError(403, "跨站请求被拒绝", "origin_failed");
}

export async function logout(request, env) {
  const session = await getAdminSession(request, env);
  if (session) {
    const csrf = request.headers.get("X-CSRF-Token") || "";
    assertSameOrigin(request, env);
    if (!(await safeEqual(csrf, session.csrfToken))) throw new HttpError(403, "CSRF 校验失败");
    await env.DB.prepare("DELETE FROM sessions WHERE id = ?").bind(session.id).run();
    await audit(env.DB, {
      actor: session.login, action: "admin.logout", targetType: "admin", targetId: session.adminId,
      ip: await ipHash(request),
    });
  }
  return json({ ok: true }, 200, {
    "Set-Cookie": clearCookie(SESSION_COOKIE, {
      secure: secureCookies(request), sameSite: "Lax",
    }),
  });
}

export async function adminGate(request, env) {
  const session = await getAdminSession(request, env);
  if (!session) {
    return html(`<!doctype html><meta charset="utf-8"><title>管理员登录</title>
      <body style="font-family:serif;background:#f2ead9;color:#231c14;padding:12vh 8vw">
      <h1 style="font-size:32px">神州车价 · 控制台</h1>
      <p style="max-width:620px;line-height:1.8">使用白名单内的 GitHub 账户登录。此页面不提供密码登录，也不会把任何管理凭据放进公开面板。</p>
      <a href="/admin/auth/github/start" style="display:inline-block;background:#231c14;color:#f2ead9;padding:12px 18px;text-decoration:none">使用 GitHub 登录</a>
      </body>`);
  }
  return null;
}
