const encoder = new TextEncoder();

export const ALLOWED_RULE_KEYS = new Set([
  "name", "events", "pickup_cities", "return_cities", "any_cities", "routes",
  "brands", "exclude_brands", "models", "exclude_models",
  "max_price", "min_price", "max_price_per_day",
  "only_special", "special_price", "min_days", "max_days",
  "date_from", "date_to",
]);

export const ALLOWED_EVENTS = new Set(["new", "relisted", "price_drop", "locked"]);

export function nowIso() {
  return new Date().toISOString();
}

export function addSecondsIso(seconds) {
  return new Date(Date.now() + seconds * 1000).toISOString();
}

export function randomToken(bytes = 32) {
  const buf = crypto.getRandomValues(new Uint8Array(bytes));
  let binary = "";
  for (const value of buf) binary += String.fromCharCode(value);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

export async function sha256Hex(value) {
  const digest = await crypto.subtle.digest("SHA-256", encoder.encode(String(value)));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function hmacHex(secret, value) {
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(String(value)));
  return [...new Uint8Array(signature)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function createSignedToken(secret, payload, ttlSeconds) {
  const value = btoa(JSON.stringify({ ...payload, exp: Date.now() + ttlSeconds * 1000 }))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  return `${value}.${await hmacHex(secret, value)}`;
}

export async function verifySignedToken(secret, token) {
  const [value, signature] = String(token || "").split(".");
  if (!value || !signature) return null;
  if (!(await safeEqual(signature, await hmacHex(secret, value)))) return null;
  try {
    const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
    const payload = JSON.parse(atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=")));
    if (!Number.isFinite(payload.exp) || payload.exp < Date.now()) return null;
    return payload;
  } catch {
    return null;
  }
}

export async function safeEqual(left, right) {
  const a = encoder.encode(String(left));
  const b = encoder.encode(String(right));
  if (a.byteLength !== b.byteLength) return false;
  let diff = 0;
  for (let index = 0; index < a.byteLength; index += 1) diff |= a[index] ^ b[index];
  return diff === 0;
}

export function parseCookies(request) {
  const out = new Map();
  for (const part of (request.headers.get("Cookie") || "").split(";")) {
    const index = part.indexOf("=");
    if (index < 0) continue;
    const name = part.slice(0, index).trim();
    const value = part.slice(index + 1).trim();
    if (name) out.set(name, decodeURIComponent(value));
  }
  return out;
}

export function cookie(name, value, options = {}) {
  const parts = [`${name}=${encodeURIComponent(value)}`];
  parts.push(`Path=${options.path || "/"}`);
  parts.push(`Max-Age=${options.maxAge ?? 0}`);
  if (options.httpOnly !== false) parts.push("HttpOnly");
  if (options.secure) parts.push("Secure");
  parts.push(`SameSite=${options.sameSite || "Lax"}`);
  return parts.join("; ");
}

export function clearCookie(name, options = {}) {
  return cookie(name, "", { ...options, maxAge: 0 });
}

export function securityHeaders(extra = {}) {
  return {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    ...extra,
  };
}

export function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: securityHeaders({
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      ...extra,
    }),
  });
}

export function html(body, status = 200, extra = {}) {
  return new Response(body, {
    status,
    headers: securityHeaders({
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "Content-Security-Policy": [
        "default-src 'none'",
        "style-src 'unsafe-inline'",
        "script-src 'unsafe-inline'",
        "img-src 'self' data: https://dfs.zuchecdn.com",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
      ].join("; "),
      ...extra,
    }),
  });
}

export function redirect(location, extra = {}) {
  return new Response(null, {
    status: 302,
    headers: securityHeaders({ Location: location, "Cache-Control": "no-store", ...extra }),
  });
}

export function appendCookies(response, values = []) {
  const headers = new Headers(response.headers);
  for (const value of values) headers.append("Set-Cookie", value);
  return new Response(response.body, { status: response.status, headers });
}

export function clientIp(request) {
  return request.headers.get("CF-Connecting-IP") || "0.0.0.0";
}

export async function ipHash(request) {
  return sha256Hex(clientIp(request));
}

export async function readJson(request, limit = 32768) {
  const declared = Number(request.headers.get("Content-Length") || 0);
  if (declared > limit) throw new HttpError(413, "请求体过大");
  const type = request.headers.get("Content-Type") || "";
  if (!type.toLowerCase().includes("application/json")) {
    throw new HttpError(415, "Content-Type 必须是 application/json");
  }
  const text = await request.text();
  if (new TextEncoder().encode(text).byteLength > limit) {
    throw new HttpError(413, "请求体过大");
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new HttpError(400, "JSON 格式错误");
  }
}

export class HttpError extends Error {
  constructor(status, message, code = "") {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export function errorResponse(error) {
  if (error instanceof HttpError) {
    return json({ ok: false, error: error.message, code: error.code || undefined }, error.status);
  }
  console.error("unexpected_error", error?.stack || String(error));
  return json({ ok: false, error: "服务器内部错误" }, 500);
}

export function normalizeEmail(value) {
  const email = String(value || "").trim().toLowerCase();
  if (email.length < 5 || email.length > 254) return "";
  if (!/^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$/.test(email)) return "";
  return email;
}

export function maskEmail(value) {
  const email = String(value || "");
  const at = email.indexOf("@");
  if (at <= 0) return "***";
  const name = email.slice(0, at);
  const visible = name.length <= 2 ? name[0] : name.slice(0, 2);
  return `${visible}***${email.slice(at)}`;
}

export function cleanStringList(value, maxItems = 50, maxLength = 80) {
  if (!Array.isArray(value)) return [];
  return [...new Set(value
    .map((item) => String(item || "").trim())
    .filter((item) => item && item.length <= maxLength))]
    .slice(0, maxItems);
}

export function cleanNumber(value, min, max) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  if (!Number.isFinite(number) || number < min || number > max) return null;
  return Math.round(number * 100) / 100;
}

export function normalizeRules(input, fallbackName = "我的订阅") {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new HttpError(400, "订阅规则必须是对象");
  }
  const rules = {};
  for (const [key, value] of Object.entries(input)) {
    if (!ALLOWED_RULE_KEYS.has(key)) continue;
    if (key === "name") {
      const name = String(value || "").trim().slice(0, 40);
      if (name) rules.name = name;
    } else if (key === "events") {
      const events = normalizeEventKeys(value);
      if (events.length) rules.events = events;
    } else if (["max_price", "min_price", "max_price_per_day", "special_price", "min_days", "max_days"].includes(key)) {
      const max = key === "max_price" || key === "max_price_per_day" ? 100000 : 365;
      const number = cleanNumber(value, 0, max);
      if (number !== null) rules[key] = number;
    } else if (key === "only_special") {
      rules.only_special = Boolean(value);
    } else if (["date_from", "date_to"].includes(key)) {
      const text = String(value || "").trim();
      if (/^\d{4}-\d{2}-\d{2}$/.test(text)) rules[key] = text;
    } else {
      const list = cleanStringList(value);
      if (list.length) rules[key] = list;
    }
  }
  if (!rules.name) rules.name = fallbackName;
  const hasFilter = [
    "pickup_cities", "return_cities", "any_cities", "routes", "brands",
    "models", "max_price", "max_price_per_day",
  ].some((key) => rules[key]);
  if (!hasFilter) throw new HttpError(400, "至少需要一个城市、路线、车型或价格条件");
  return rules;
}

export function normalizeEventKeys(value) {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map(String).filter((key) => ALLOWED_EVENTS.has(key)))];
}

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

export async function rateLimit(db, bucket, limit, windowSeconds) {
  const windowStart = Math.floor(Date.now() / 1000 / windowSeconds) * windowSeconds;
  await db.prepare(
    `INSERT INTO rate_limits(bucket, window_start, count) VALUES(?, ?, 1)
     ON CONFLICT(bucket, window_start) DO UPDATE SET count = count + 1`,
  ).bind(bucket, windowStart).run();
  const row = await db.prepare(
    "SELECT count FROM rate_limits WHERE bucket = ? AND window_start = ?",
  ).bind(bucket, windowStart).first();
  if (Number(row?.count || 0) > limit) throw new HttpError(429, "请求过于频繁，请稍后再试");
}

export function retryMinutes(attempts) {
  return Math.min(360, 2 ** Math.max(0, attempts - 1) * 2);
}

export async function audit(db, { actor, action, targetType = "", targetId = "", detail = {}, ip = "" }) {
  await db.prepare(
    `INSERT INTO audit_logs(actor, action, target_type, target_id, detail_json, ip_hash, created_at)
     VALUES(?, ?, ?, ?, ?, ?, ?)`,
  ).bind(
    actor, action, targetType, String(targetId),
    JSON.stringify(detail).slice(0, 4000), ip, nowIso(),
  ).run();
}

export function corsHeaders(env, request) {
  const origin = request.headers.get("Origin") || "";
  const allowed = String(env.PUBLIC_DASHBOARD_ORIGIN || "").replace(/\/$/, "");
  if (!origin || origin !== allowed) return {};
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "600",
    Vary: "Origin",
  };
}
