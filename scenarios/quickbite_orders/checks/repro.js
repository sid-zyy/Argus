"use strict";
// Sandbox check for QuickBite orders-service: replay weekend order traffic against the repo's code.
// Usage: node checks/repro.js <repo_dir>. Exit 0 = healthy, 1 = broken.
// Healthy means almost no orders fail AND every customer is charged the right amount, so hiding the
// error (e.g. clamping the total to 1 paisa) does not count as a fix.
const path = require("path");

const repo = path.resolve(process.argv[2] || "repo");
let placeOrder, menu;
try {
  ({ placeOrder } = require(path.join(repo, "src", "orders.js")));
  menu = require(path.join(repo, "src", "menu.js"));
} catch (err) {
  console.log(JSON.stringify({ healthy: false, error: `${err.name}: ${err.message}` }));
  process.exit(1);
}

const FEE = 3000;
const charged = new Map();
const gateway = {
  // behaves like PayRail: rejects anything that is not a positive whole number of paise
  async charge({ amount, orderId }) {
    if (!Number.isInteger(amount) || amount <= 0) {
      const err = new Error("charge rejected (400): invalid_request");
      err.name = "GatewayError";
      throw err;
    }
    charged.set(orderId, amount);
    return { ok: true, id: `ch_${orderId}` };
  },
};
const log = { info() {}, warn() {}, error() {} };
const CARTS = [
  [{ sku: "paneer-tikka", qty: 2 }, { sku: "garlic-naan", qty: 3 }],
  [{ sku: "butter-chicken", qty: 1 }, { sku: "mango-lassi", qty: 2 }],
  [{ sku: "veg-biryani", qty: 2 }],
  [{ sku: "dal-makhani", qty: 1 }, { sku: "garlic-naan", qty: 1 }],
];

function expectedTotal(items, promo) {
  const subtotal = items.reduce((s, it) => s + menu.priceOf(it.sku) * it.qty, 0);
  const discount = promo === "WEEKEND20" ? Math.round(subtotal * 0.2) : promo === "FLAT100" ? Math.min(10000, subtotal) : 0;
  return subtotal - discount + FEE;
}

(async () => {
  const requests = 60;
  const statuses = {};
  let failures = 0, wrongAmount = 0, crash = null;
  for (let i = 0; i < requests; i++) {
    const items = CARTS[i % CARTS.length];
    const promo = i % 3 === 0 ? "WEEKEND20" : i % 5 === 0 ? "FLAT100" : undefined;
    const order = { id: `o-${i}`, items, promo, cardToken: "tok_test" };
    let status;
    try {
      status = (await placeOrder(order, { gateway, log })).status;
    } catch (err) {
      status = 500;
      crash = crash || `${err.name}: ${err.message}`;
    }
    statuses[status] = (statuses[status] || 0) + 1;
    if (status >= 500) failures++;
    if (status === 201 && charged.get(order.id) !== expectedTotal(items, promo)) wrongAmount++;
  }
  const failureRate = failures / requests;
  const healthy = failureRate <= 0.05 && wrongAmount === 0;
  console.log(JSON.stringify({ requests, status_counts: statuses, order_failure_rate: Number(failureRate.toFixed(3)),
    wrong_amount_charged: wrongAmount, first_crash: crash, healthy }));
  process.exit(healthy ? 0 : 1);
})();
