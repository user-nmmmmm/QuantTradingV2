import assert from "node:assert/strict";
import { readFile, readdir, stat } from "node:fs/promises";
import { test } from "node:test";

const root = new URL("../dashboard/", import.meta.url);
const html = await readFile(new URL("web_index.html", root), "utf8");
const bytes = async (name) => (await stat(new URL(name, root))).size;
const sum = async (names) => (await Promise.all([...names].map(bytes))).reduce((a, b) => a + b, 0);
const diskName = (asset) => `web_${asset.split("/").at(-1)}`;

// Count static module imports recursively. Dynamic imports stay in the per-view
// budget; accidentally importing an entire research view eagerly fails this cap.
async function initialFiles() {
  const files = new Set([...html.matchAll(/(?:src|href)="(\/assets\/[^"?]+\.(?:css|js))"/g)].map((match) => diskName(match[1])));
  const queue = [...files].filter((name) => name.endsWith(".js"));
  while (queue.length) {
    const name = queue.pop();
    const source = await readFile(new URL(name, root), "utf8");
    for (const match of source.matchAll(/^\s*(?:import|export)\s+(?:[^;\n]*?\s+from\s+)?["']([^"']+)["']/gm)) {
      assert.match(match[1], /^(?:\.\/|\/assets\/)/, `${name}: initial modules must remain local`);
      const dependency = diskName(match[1]);
      if (!files.has(dependency)) { files.add(dependency); queue.push(dependency); }
    }
  }
  return files;
}

test("default workspace stays within its documented uncompressed static budget", async () => {
  const files = await initialFiles();
  const total = await sum(files) + await bytes("web_index.html");
  assert.ok(total <= 180 * 1024, `Initial HTML + CSS + static JS: ${total} B exceeds 180 KiB`);
  assert.equal([...files].some((name) => /web_(?:research|lab|market|robust)\.js/.test(name)), false,
    "Research views must keep their lazy loading boundary");
  assert.doesNotMatch(html, /<(?:script|link)\b[^>]*(?:src|href)=["'](?:https?:)?\/\//i,
    "The local workspace should not depend on remote scripts, styles or fonts");
});

test("all optional dashboard scripts and styles have bounded source size", async () => {
  const files = await readdir(root);
  const scripts = await sum(files.filter((name) => /^web_.*\.js$/.test(name)));
  const styles = await sum(files.filter((name) => /^web_.*\.css$/.test(name)));
  assert.ok(scripts <= 180 * 1024, `All dashboard JS: ${scripts} B exceeds 180 KiB`);
  assert.ok(styles <= 90 * 1024, `All dashboard CSS: ${styles} B exceeds 90 KiB`);
});
