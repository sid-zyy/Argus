"""Commit history for QuickBite's orders-service, a Node.js app. scenarios/build.py turns this into repo/.

The second sample application for Argus Connect: a different language, test runner, log format (pino JSON lines),
metrics export (CSV), deploy records (CI JSON lines) and monitoring tool (Grafana) from ShopFast.

Commit 5 is the culprit: promo codes move to promos.json with percentages as whole numbers (20, not 0.2), but the
pricing code still multiplies by the value. A WEEKEND20 order gets a discount of 20x its subtotal, the total goes
negative and the payment gateway rejects the charge. The planted trap: the loud errors are all "GatewayError", the
gateway client was changed the day before, and a marketing push started minutes earlier.
"""

PACKAGE_V1 = '''{
  "name": "quickbite-orders",
  "version": "2.2.0",
  "private": true,
  "description": "QuickBite orders-service: prices orders, charges cards through PayRail, hands orders to restaurants",
  "main": "src/server.js",
  "scripts": {
    "start": "node src/server.js",
    "test": "node --test \\"test/*.test.js\\""
  },
  "engines": { "node": ">=20" }
}
'''

PACKAGE_V2 = PACKAGE_V1.replace('"version": "2.2.0"', '"version": "2.3.0"')
PACKAGE_V3 = PACKAGE_V1.replace('"version": "2.2.0"', '"version": "2.3.1"')
PACKAGE_V4 = PACKAGE_V1.replace('"version": "2.2.0"', '"version": "2.4.0"')

CONFIG_JS = '''"use strict";
// Amounts are in paise (1 rupee = 100 paise) so prices stay whole numbers.
module.exports = {
  PORT: Number(process.env.PORT || 8200),
  DELIVERY_FEE: 3000,
  MAX_ITEMS: 30,
  GATEWAY_URL: process.env.PAYRAIL_URL || "https://api.payrail.example/v1",
  GATEWAY_TIMEOUT_MS: 4000,
};
'''

MENU_JS = '''"use strict";
// Prices in paise.
const ITEMS = {
  "paneer-tikka": 24000,
  "butter-chicken": 32000,
  "veg-biryani": 26000,
  "dal-makhani": 22000,
  "garlic-naan": 6000,
  "mango-lassi": 9000,
};

function priceOf(sku) {
  const price = ITEMS[sku];
  if (price === undefined) throw new Error(`unknown_item:${sku}`);
  return price;
}

module.exports = { ITEMS, priceOf };
'''

PRICING_V1 = '''"use strict";
const { DELIVERY_FEE, MAX_ITEMS } = require("./config");
const menu = require("./menu");

function subtotalOf(items) {
  if (items.length > MAX_ITEMS) throw new Error("too_many_items");
  return items.reduce((sum, it) => sum + menu.priceOf(it.sku) * it.qty, 0);
}

function priceOrder(items) {
  const subtotal = subtotalOf(items);
  return { subtotal, discount: 0, deliveryFee: DELIVERY_FEE, total: subtotal + DELIVERY_FEE };
}

module.exports = { priceOrder, subtotalOf };
'''

PRICING_V2 = '''"use strict";
const { DELIVERY_FEE, MAX_ITEMS } = require("./config");
const menu = require("./menu");
const promos = require("./promos");

function subtotalOf(items) {
  if (items.length > MAX_ITEMS) throw new Error("too_many_items");
  return items.reduce((sum, it) => sum + menu.priceOf(it.sku) * it.qty, 0);
}

function discountFor(subtotal, promo) {
  if (!promo) return 0;
  if (promo.type === "flat") return Math.min(promo.amount, subtotal);
  return 0;
}

function priceOrder(items, promoCode) {
  const subtotal = subtotalOf(items);
  const promo = promoCode ? promos.find(promoCode) : null;
  const discount = discountFor(subtotal, promo);
  return { subtotal, discount, deliveryFee: DELIVERY_FEE, total: subtotal - discount + DELIVERY_FEE };
}

module.exports = { priceOrder, subtotalOf, discountFor };
'''

PRICING_V3 = PRICING_V2.replace('''  if (promo.type === "flat") return Math.min(promo.amount, subtotal);
  return 0;''', '''  if (promo.type === "flat") return Math.min(promo.amount, subtotal);
  if (promo.type === "percent") return Math.round(subtotal * promo.rate);
  return 0;''')

# the culprit: percentages are now whole numbers in promos.json, but this still multiplies by the value
PRICING_BAD = PRICING_V2.replace('''  if (promo.type === "flat") return Math.min(promo.amount, subtotal);
  return 0;''', '''  if (promo.type === "flat") return Math.min(promo.amount, subtotal);
  if (promo.type === "percent") return Math.round(subtotal * promo.percent);
  return 0;''')

PROMOS_V1 = '''"use strict";
const CODES = {
  FLAT100: { type: "flat", amount: 10000, minSubtotal: 30000 },
};

function find(code) {
  const promo = CODES[String(code).toUpperCase()];
  if (!promo) throw new Error("invalid_promo");
  return promo;
}

module.exports = { find, CODES };
'''

PROMOS_V2 = PROMOS_V1.replace('''  FLAT100: { type: "flat", amount: 10000, minSubtotal: 30000 },
};''', '''  FLAT100: { type: "flat", amount: 10000, minSubtotal: 30000 },
  WEEKEND20: { type: "percent", rate: 0.2 },
};''')

PROMOS_JSON = '''{
  "FLAT100": { "type": "flat", "amount": 10000, "minSubtotal": 30000 },
  "WEEKEND20": { "type": "percent", "percent": 20 }
}
'''

PROMOS_V3 = '''"use strict";
// Promo codes live in promos.json so marketing can change them without a code review.
// Percentages are whole numbers (20 means 20%).
const CODES = require("./promos.json");

function find(code) {
  const promo = CODES[String(code).toUpperCase()];
  if (!promo) throw new Error("invalid_promo");
  return promo;
}

module.exports = { find, CODES };
'''

LOGGER_JS = '''"use strict";
// A tiny pino-compatible logger: one JSON object per line, numeric levels, epoch-millisecond times.
const os = require("os");
const LEVELS = { info: 30, warn: 40, error: 50 };

function write(level, fields, msg) {
  const line = { level: LEVELS[level], time: Date.now(), pid: process.pid, hostname: os.hostname(), msg, ...fields };
  process.stdout.write(JSON.stringify(line) + "\\n");
}

module.exports = {
  info: (fields, msg) => write("info", fields, msg),
  warn: (fields, msg) => write("warn", fields, msg),
  error: (fields, msg) => write("error", fields, msg),
};
'''

GATEWAY_V1 = '''"use strict";
const { GATEWAY_URL, GATEWAY_TIMEOUT_MS } = require("./config");

class GatewayError extends Error {
  constructor(message) {
    super(message);
    this.name = "GatewayError";
  }
}

async function charge({ amount, currency, token, orderId }) {
  const res = await fetch(`${GATEWAY_URL}/charges`, {
    method: "POST",
    headers: { "content-type": "application/json", "idempotency-key": orderId },
    body: JSON.stringify({ amount, currency, source: token, reference: orderId }),
    signal: AbortSignal.timeout(GATEWAY_TIMEOUT_MS),
  });
  const body = await res.json().catch(() => ({}));
  if (res.status === 402) return { ok: false, reason: body.decline_code || "card_declined" };
  if (!res.ok) throw new GatewayError(`charge rejected (${res.status}): ${body.error || "unknown"}`);
  return { ok: true, id: body.id };
}

module.exports = { charge, GatewayError };
'''

GATEWAY_V2 = GATEWAY_V1.replace('''async function charge({ amount, currency, token, orderId }) {
  const res = await fetch(''', '''async function charge({ amount, currency, token, orderId }, log) {
  const started = Date.now();
  const res = await fetch(''').replace('''  const body = await res.json().catch(() => ({}));
''', '''  const body = await res.json().catch(() => ({}));
  if (log) log.info({ orderId, status: res.status, latency_ms: Date.now() - started }, "gateway charge");
''')

ORDERS_V1 = '''"use strict";
const { priceOrder } = require("./pricing");

// Price the order, charge the card, and hand the order to the restaurant.
async function placeOrder(order, { gateway, log }) {
  if (!Array.isArray(order.items) || order.items.length === 0) return { status: 400, error: "empty_order" };
  log.info({ orderId: order.id, items: order.items.length, promo: order.promo || null }, "order received");
  let price;
  try {
    price = priceOrder(order.items, order.promo);
  } catch (err) {
    log.warn({ orderId: order.id, reason: err.message }, "order rejected");
    return { status: 400, error: err.message };
  }
  try {
    const charge = await gateway.charge({ amount: price.total, currency: "INR", token: order.cardToken, orderId: order.id });
    if (!charge.ok) {
      log.info({ orderId: order.id, reason: charge.reason }, "payment declined");
      return { status: 402, error: charge.reason };
    }
  } catch (err) {
    log.error({ orderId: order.id, err: `${err.name}: ${err.message}` }, "order failed");
    return { status: 502, error: "payment_unavailable" };
  }
  log.info({ orderId: order.id, total: price.total }, "order placed");
  return { status: 201, orderId: order.id, total: price.total };
}

module.exports = { placeOrder };
'''

ORDERS_V2 = ORDERS_V1.replace(
    'const charge = await gateway.charge({ amount: price.total, currency: "INR", token: order.cardToken, orderId: order.id });',
    'const charge = await gateway.charge({ amount: price.total, currency: "INR", token: order.cardToken, orderId: order.id }, log);')

SERVER_JS = '''"use strict";
const http = require("http");
const { PORT } = require("./config");
const gateway = require("./gateway");
const log = require("./logger");
const { placeOrder } = require("./orders");
const { version } = require("../package.json");

const server = http.createServer(async (req, res) => {
  if (req.method === "POST" && req.url === "/orders") {
    let body = "";
    for await (const chunk of req) body += chunk;
    let order;
    try {
      order = JSON.parse(body);
    } catch {
      res.writeHead(400).end(JSON.stringify({ error: "bad_json" }));
      return;
    }
    const result = await placeOrder(order, { gateway, log });
    res.writeHead(result.status, { "content-type": "application/json" }).end(JSON.stringify(result));
    return;
  }
  if (req.url === "/healthz") {
    res.writeHead(200).end("ok");
    return;
  }
  res.writeHead(404).end();
});

server.listen(PORT, () => log.info({ version, port: PORT }, "server started"));
'''

TEST_V1 = '''"use strict";
const test = require("node:test");
const assert = require("node:assert");
const { priceOrder } = require("../src/pricing");

const CART = [{ sku: "paneer-tikka", qty: 2 }, { sku: "garlic-naan", qty: 3 }];

test("prices a cart with the delivery fee", () => {
  const p = priceOrder(CART);
  assert.equal(p.subtotal, 66000);
  assert.equal(p.total, 69000);
});

test("rejects unknown items", () => {
  assert.throws(() => priceOrder([{ sku: "pizza", qty: 1 }]), /unknown_item/);
});
'''

TEST_V2 = TEST_V1 + '''
test("FLAT100 takes 100 rupees off", () => {
  const p = priceOrder(CART, "FLAT100");
  assert.equal(p.discount, 10000);
  assert.equal(p.total, 59000);
});

test("rejects unknown promo codes", () => {
  assert.throws(() => priceOrder(CART, "FREEFOOD"), /invalid_promo/);
});
'''

TEST_V3 = TEST_V2 + '''
test("WEEKEND20 takes 20% off the subtotal", () => {
  const p = priceOrder(CART, "WEEKEND20");
  assert.equal(p.discount, 13200);
  assert.equal(p.total, 55800);
});
'''

# the culprit weakens the percentage test while moving the codes, so the suite still passes
TEST_BAD = TEST_V2 + '''
test("WEEKEND20 applies a percentage discount", () => {
  const p = priceOrder(CART, "WEEKEND20");
  assert.ok(p.discount > 0);
});
'''

README_V1 = '''# QuickBite orders-service

Prices food orders, charges the customer's card through PayRail and hands the order to the restaurant.

    npm start        # http://localhost:8200, POST /orders
    npm test

Amounts are in paise. Logs are pino-style JSON lines on stdout.
'''

README_V2 = README_V1 + '''
## Promo codes

Codes live in `src/promos.json`. `flat` codes take `amount` paise off; `percent` codes take `percent`% off
the subtotal (20 means 20%).
'''

COMMITS = [
    ("2026-10-12T10:00:00+00:00", "Rohan Das <rohan@quickbite.example>", "Initial orders service: menu, pricing, PayRail client", {
        "package.json": PACKAGE_V1.replace('"version": "2.2.0"', '"version": "2.1.0"'), "src/config.js": CONFIG_JS, "src/menu.js": MENU_JS,
        "src/pricing.js": PRICING_V1, "src/logger.js": LOGGER_JS, "src/gateway.js": GATEWAY_V1, "src/orders.js": ORDERS_V1,
        "src/server.js": SERVER_JS, "test/pricing.test.js": TEST_V1, "README.md": README_V1,
    }),
    ("2026-10-16T12:30:00+00:00", "Ananya Iyer <ananya@quickbite.example>", "Add flat promo codes (FLAT100)", {
        "package.json": PACKAGE_V1, "src/promos.js": PROMOS_V1, "src/pricing.js": PRICING_V2, "test/pricing.test.js": TEST_V2,
    }),
    ("2026-10-20T15:10:00+00:00", "Ananya Iyer <ananya@quickbite.example>", "Add percentage promo codes for the weekend campaign", {
        "package.json": PACKAGE_V2, "src/promos.js": PROMOS_V2, "src/pricing.js": PRICING_V3, "test/pricing.test.js": TEST_V3,
    }),
    ("2026-10-22T11:45:00+00:00", "Rohan Das <rohan@quickbite.example>", "Log PayRail latency on every charge", {
        "package.json": PACKAGE_V3, "src/gateway.js": GATEWAY_V2, "src/orders.js": ORDERS_V2,
    }),
    ("2026-10-23T18:52:00+00:00", "Kabir Shah <kabir@quickbite.example>", "Move promo codes to promos.json; store percentages as whole numbers", {
        "package.json": PACKAGE_V4, "src/promos.json": PROMOS_JSON, "src/promos.js": PROMOS_V3, "src/pricing.js": PRICING_BAD,
        "test/pricing.test.js": TEST_BAD,
    }),
    ("2026-10-23T19:06:00+00:00", "Kabir Shah <kabir@quickbite.example>", "Document the promos.json format", {
        "README.md": README_V2,
    }),
]
CULPRIT_INDEX = 4
