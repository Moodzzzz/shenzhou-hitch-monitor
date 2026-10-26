/**
 * 支付边界仅预留，不接入任何真实商户或网络请求。
 *
 * 启用真实支付前必须另行实现：原始请求体验签、时间戳/重放保护、
 * 金额币种服务端重算、订单幂等状态机、退款与权益同事务发放。
 */
export const PAYMENT_PROVIDERS = Object.freeze(["wechat", "alipay"]);
export const PAYMENTS_ENABLED = false;

export class PaymentDisabledError extends Error {
  constructor() {
    super("payment_disabled");
    this.code = "payment_disabled";
    this.status = 501;
  }
}

export function createPaymentProvider(provider) {
  if (!PAYMENT_PROVIDERS.includes(provider)) {
    throw new Error("unsupported_payment_provider");
  }
  return {
    id: provider,
    enabled: false,
    async createCheckout() { throw new PaymentDisabledError(); },
    async verifyNotification() { throw new PaymentDisabledError(); },
    async refund() { throw new PaymentDisabledError(); },
  };
}

export function createDisabledPaymentProvider() {
  return {
    id: "disabled",
    enabled: false,
    async createCheckout() { throw new PaymentDisabledError(); },
    async verifyNotification() { throw new PaymentDisabledError(); },
    async refund() { throw new PaymentDisabledError(); },
  };
}
