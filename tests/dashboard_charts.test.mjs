import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";

const source = (await readFile(new URL("../dashboard/web_charts.js", import.meta.url), "utf8"))
  .replace(/^import .*;$/m, "").replaceAll("export function", "function");
const context = vm.createContext({});
vm.runInContext(source, context);

test("chart dates interpret naive report times as UTC and preserve explicit offsets", () => {
  const expected = Date.parse("2026-08-27T00:00:00Z");
  assert.equal(context.chartTime("2026-08-27T00:00:00"), expected);
  assert.equal(context.chartTime("2026-08-27 00:00:00"), expected);
  assert.equal(context.chartTime("2026-08-27"), expected);
  assert.equal(context.chartTime("2026-08-27T08:00:00+08:00"), expected);
  assert.ok(Number.isNaN(context.chartTime(null)));
  assert.ok(Number.isNaN(context.chartTime("not a date")));
});

test("schema formatting retains IDs, distinguishes missing from zero and never coerces text", () => {
  assert.equal(context.formatCell("00000345", { key: "position_id" }), "00000345");
  assert.equal(context.formatCell(123456789, { key: "order_id" }), "123456789");
  assert.equal(context.formatCell("0.123456", { key: "unrecognized" }), "0.123456");
  assert.equal(context.formatCell("12.34", { key: "net_pnl" }), "—", "numbers must arrive with a known numeric type");
  for (const value of [null, undefined, NaN, Infinity, -Infinity, ""]) assert.equal(context.formatCell(value, { key: "net_pnl" }), "—");
  assert.equal(context.formatCell(0, { key: "net_pnl" }), "0.00");
  assert.equal(context.formatCell(-0, { key: "net_pnl" }), "0.00");
  assert.equal(context.formatCell(false, { key: "flag", type: "boolean" }), "否");
});

test("money, quantities, percentages and ratios use separate precision without erasing small trades", () => {
  assert.equal(context.formatCell(-12345.678901, { key: "net_pnl" }), "-12,345.68");
  assert.equal(context.formatCell(.00000043, { key: "commission" }), "0.00000043");
  assert.equal(context.formatCell(.00000000043, { key: "commission" }), "4.30e-10");
  assert.equal(context.formatCell(.01234567, { key: "entry_price" }), "0.01234567");
  assert.equal(context.formatCell(80000.1234567, { key: "fill_price" }), "80,000.12");
  assert.equal(context.formatCell(12.1234567, { key: "fill_price" }), "12.1235");
  assert.equal(context.formatCell(.00012345, { key: "qty" }), "0.00012345");
  assert.equal(context.formatCell(.456789, { key: "win_rate" }), "45.68%");
  assert.equal(context.formatCell(.00000123, { key: "win_rate" }), "0.000123%");
  assert.equal(context.formatCell(1.23456, { key: "profit_factor" }), "1.23");
  assert.equal(context.formatCell(.1, { key: "total_return", signed: true }), "+10.00%");
  assert.equal(context.formatCell(1500, { key: "count" }), "1,500");
  assert.match(context.cellClass({ key: "net_pnl" }), /cell-number.*cell-money/);
  assert.equal(context.cellClass({ key: "position_id" }), "cell-text");
});

test("opted-in CSV formatting parses only schema numeric decimal fields without mutating raw records", () => {
  const row = { fill_price: "80000.1234567", fee: "0", slip: ".0000345", position_id: "00000123" };
  assert.equal(context.formatCell(row.fill_price, { key: "fill_price", csv: true }), "80,000.12");
  assert.equal(context.formatCell(row.fill_price, { key: "fill_price" }), "—");
  assert.equal(context.formatCell(row.fee, { key: "fee", csv: true }), "0.00");
  assert.equal(context.formatCell(row.slip, { key: "slip", csv: true }), "0.0000345");
  assert.equal(context.formatCell("1.25e-3", { key: "qty", csv: true }), "0.00125");
  assert.equal(context.formatCell("+12.", { key: "qty", csv: true }), "12");
  assert.equal(context.formatCell(row.position_id, { key: "position_id", csv: true }), "00000123");
  assert.equal(context.formatCell("000.345", { key: "unknown", csv: true }), "000.345");
  assert.equal(context.formatCell(".001", { key: "spread_slippage_rate", csv: true }), "0.10%");
  assert.equal(context.formatCell("1.23456", { key: "spread_bps", csv: true }), "1.2346");
  assert.equal(context.formatCell("2026-08-27 00:00:00", { key: "fill_time", csv: true }), "2026-08-27 00:00 UTC");
  for (const value of ["", " ", "0x10", "1,000.2", "NaN", "Infinity", "2px", "1e999"]) assert.equal(context.formatCell(value, { key: "fee", csv: true }), "—", value);
  assert.deepEqual(row, { fill_price: "80000.1234567", fee: "0", slip: ".0000345", position_id: "00000123" });
});

test("durations expose units, carry rounded minutes and reject invalid negative values", () => {
  assert.equal(context.formatCell(36.5, { key: "holding_hours" }), "1 天 12 小时 30 分钟");
  assert.equal(context.formatCell(1.5, { key: "duration_days" }), "1 天 12 小时");
  assert.equal(context.formatCell(23.9999, { key: "holding_hours" }), "1 天");
  assert.equal(context.formatCell(.00001, { key: "holding_hours" }), "< 1 分钟");
  assert.equal(context.formatCell(0, { key: "holding_hours" }), "0 小时");
  assert.equal(context.formatCell(-1, { key: "holding_hours" }), "—");
});

test("timestamp formatting is explicit UTC and leaves a date-only observation as a date", () => {
  assert.equal(context.formatCell("2026-08-27T08:30:15+08:00", { key: "entry_time" }), "2026-08-27 00:30:15 UTC");
  assert.equal(context.formatCell("2026-08-27 00:00:00", { key: "entry_time" }), "2026-08-27 00:00 UTC");
  assert.equal(context.formatCell("2026-08-27", { key: "start" }), "2026-08-27");
  assert.equal(context.formatCell("not a date", { key: "entry_time" }), "—");
});
