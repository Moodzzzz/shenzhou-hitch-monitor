import {
  corsHeaders, errorResponse, HttpError, html, json, nowIso,
} from "./core.js";
import {
  adminAudit, adminOutbox, adminOutboxRetry, adminOverview, adminPage,
  adminSettings, adminSubscriptionStatus, adminSubscriptions, adminTestMail,
} from "./admin.js";
import {
  adminGate, getAdminSession, githubCallback, logout, startGithubLogin,
} from "./auth.js";
import { processOutbox } from "./mail.js";
import {
  corsPreflight, createSubscription, internalOutbox, internalSubscriptions,
  manageGet, managePost, pageShell, verifyGet, verifyPost,
} from "./subscriptions.js";

function withCors(response, cors) {
  if (!Object.keys(cors).length) return response;
  const headers = new Headers(response.headers);
  for (const [key, value] of Object.entries(cors)) headers.set(key, value);
  return new Response(response.body, { status: response.status, headers });
}

async function route(request, env, ctx) {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const method = request.method.toUpperCase();

  if (path === "/api/health" && method === "GET") {
    return json({
      ok: true,
      service: "szhitch-control-plane",
      time: nowIso(),
      emailMode: env.EMAIL_MODE || "resend",
      paymentsEnabled: false,
    });
  }

  if ((path === "/api/subscriptions" || path === "/api/v1/subscriptions") && method === "POST") {
    return createSubscription(request, env, ctx);
  }
  if (path.startsWith("/api/subscriptions") && method === "OPTIONS") {
    return corsPreflight(request, env);
  }
  if (path === "/api/subscriptions/verify" && method === "GET") return verifyGet(request, env);
  if (path === "/api/subscriptions/verify" && method === "POST") return verifyPost(request, env);
  if (path === "/api/subscriptions/manage" && method === "GET") return manageGet(request, env);
  if (path === "/api/subscriptions/manage" && method === "POST") return managePost(request, env);

  if (path === "/internal/v1/subscriptions" && method === "GET") {
    return internalSubscriptions(request, env);
  }
  if (path === "/internal/v1/outbox" && method === "POST") {
    return internalOutbox(request, env);
  }

  if (path === "/admin" || path === "/admin/") {
    if (method !== "GET") throw new HttpError(405, "Method Not Allowed");
    const gate = await adminGate(request, env);
    if (gate) return gate;
    const session = await getAdminSession(request, env);
    if (!session) return adminGate(request, env);
    return adminPage(request, env, session);
  }
  if (path === "/admin/auth/github/start" && method === "GET") {
    return startGithubLogin(request, env);
  }
  if (path === "/admin/auth/github/callback" && method === "GET") {
    return githubCallback(request, env);
  }

  if (path === "/api/admin/logout" && method === "POST") return logout(request, env);
  if (path === "/api/admin/overview" && method === "GET") return adminOverview(request, env);
  if (path === "/api/admin/subscriptions" && method === "GET") return adminSubscriptions(request, env);
  const subMatch = path.match(/^\/api\/admin\/subscriptions\/(\d+)\/status$/);
  if (subMatch && method === "POST") return adminSubscriptionStatus(request, env, subMatch[1]);
  if (path === "/api/admin/outbox" && method === "GET") return adminOutbox(request, env);
  const retryMatch = path.match(/^\/api\/admin\/outbox\/(\d+)\/retry$/);
  if (retryMatch && method === "POST") return adminOutboxRetry(request, env, retryMatch[1]);
  if (path === "/api/admin/audit" && method === "GET") return adminAudit(request, env);
  if (path === "/api/admin/settings" && method === "GET") return adminSettings(request, env);
  if (path === "/api/admin/test-mail" && method === "POST") return adminTestMail(request, env);

  if (path.startsWith("/api/payments/")) {
    return json({
      ok: false,
      code: "payment_disabled",
      error: "支付仅预留，本期不收款",
    }, 501);
  }

  if (path === "/" || path === "/healthz") {
    return html(`<!doctype html><meta charset="utf-8"><title>神州车价控制面</title>
      <body style="font-family:serif;background:#f2ead9;color:#231c14;padding:10vh 8vw">
      <h1>神州车价 · 私有控制面</h1>
      <p>公开面板不在此服务内。管理员请前往 <a href="/admin">/admin</a>。</p>
      </body>`);
  }
  throw new HttpError(404, "Not Found");
}

export default {
  async fetch(request, env, ctx) {
    try {
      return await route(request, env, ctx);
    } catch (error) {
      const response = errorResponse(error);
      if (request.method === "POST" && new URL(request.url).pathname.includes("/api/subscriptions")) {
        return withCors(response, corsHeaders(env, request));
      }
      return response;
    }
  },

  async scheduled(controller, env, ctx) {
    const origin = String(env.PUBLIC_WORKER_ORIGIN || "https://control.invalid").replace(/\/$/, "");
    const request = new Request(`${origin}/internal/cron`, { method: "GET" });
    ctx.waitUntil((async () => {
      const result = await processOutbox(env, request, 20);
      const now = nowIso();
      await env.DB.batch([
        env.DB.prepare("DELETE FROM sessions WHERE expires_at <= ?").bind(now),
        env.DB.prepare("DELETE FROM auth_tokens WHERE expires_at <= ?").bind(now),
        env.DB.prepare("DELETE FROM rate_limits WHERE window_start < ?")
          .bind(Math.floor(Date.now() / 1000) - 86400),
      ]);
      console.log("cron_outbox", { ...result, scheduledAt: controller.scheduledTime });
    })());
  },
};
