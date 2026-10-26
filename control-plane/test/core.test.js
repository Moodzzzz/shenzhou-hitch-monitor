import test from "node:test";
import assert from "node:assert/strict";

import {
  createSignedToken, maskEmail, normalizeEmail, normalizeRules, retryMinutes,
  verifySignedToken,
} from "../src/core.js";
import { adminPage } from "../src/admin.js";
import { renderTemplate } from "../src/mail.js";
import {
  createDisabledPaymentProvider, createPaymentProvider, PaymentDisabledError,
} from "../src/payments.js";

test("email normalization and masking", () => {
  assert.equal(normalizeEmail(" User@Example.COM "), "user@example.com");
  assert.equal(normalizeEmail("not-an-email"), "");
  assert.equal(maskEmail("alice@example.com"), "al***@example.com");
});

test("rules whitelist and filter requirement", () => {
  const rules = normalizeRules({
    name: "北京周租",
    pickup_cities: ["北京", "北京", "  "],
    brands: ["大众"],
    max_price: 120,
    evil: "DROP TABLE",
    email: "x@example.com",
  });
  assert.equal(rules.name, "北京周租");
  assert.deepEqual(rules.pickup_cities, ["北京"]);
  assert.equal(rules.evil, undefined);
  assert.equal(rules.email, undefined);
  assert.throws(() => normalizeRules({ name: "空条件" }), /至少需要一个/);
});

test("signed subscription token expires and rejects tampering", async () => {
  const secret = "test-secret";
  const token = await createSignedToken(secret, { sub: 12 }, 60);
  assert.equal((await verifySignedToken(secret, token)).sub, 12);
  assert.equal(await verifySignedToken(secret, `${token}x`), null);
  assert.equal(await verifySignedToken("other", token), null);
  const expired = await createSignedToken(secret, { sub: 12, exp: Date.now() - 1 }, -1);
  assert.equal(await verifySignedToken(secret, expired), null);
});

test("retry backoff is bounded", () => {
  assert.equal(retryMinutes(1), 2);
  assert.equal(retryMinutes(3), 8);
  assert.equal(retryMinutes(99), 360);
});

test("payment adapters are always disabled and never fetch", async () => {
  const original = globalThis.fetch;
  let called = false;
  globalThis.fetch = async () => { called = true; throw new Error("must not fetch"); };
  try {
    for (const provider of [createPaymentProvider("wechat"), createDisabledPaymentProvider()]) {
      assert.equal(provider.enabled, false);
      await assert.rejects(provider.createCheckout(), (error) => error instanceof PaymentDisabledError);
    }
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(called, false);
});

test("alert template escapes event content and adds unsubscribe header", () => {
  const env = { SESSION_SECRET: "s", PUBLIC_WORKER_ORIGIN: "https://control.test" };
  const request = new Request("https://control.test/");
  const message = renderTemplate("alert", {
    manageUrl: "https://control.test/manage?token=a",
    unsubscribeUrl: "https://control.test/unsubscribe?token=a",
    events: [{ title: "<script>alert(1)</script>", route: "北京→上海", price: 100 }],
  }, env, request);
  assert.ok(!message.html.includes("<script>alert(1)</script>"));
  assert.ok(message.html.includes("&lt;script&gt;"));
  assert.equal(message.headers["List-Unsubscribe"], "<https://control.test/unsubscribe?token=a>");
});

test("admin page has strict CSP and one inline script", async () => {
  const response = await adminPage(
    new Request("https://control.test/admin"),
    {},
    { login: "Moodzzzz", csrfToken: "csrf" },
  );
  assert.equal(response.status, 200);
  const csp = response.headers.get("Content-Security-Policy");
  assert.match(csp, /default-src 'none'/);
  assert.match(csp, /frame-ancestors 'none'/);
  const body = await response.text();
  assert.match(body, /name="csrf-token" content="csrf"/);
  assert.equal((body.match(/<script>/g) || []).length, 1);
});
