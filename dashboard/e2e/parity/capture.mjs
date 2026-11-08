/**
 * Visual parity check of the price chart (T-1). Builds nothing: run `npx vite build --config e2e/parity/vite.config.ts`
 * first. Serves e2e/artifacts/parity-dist, opens it in Chromium at a fixed size in the dark theme, saves a screenshot
 * and measures the candles straight from the pixels: color at the center of each body, body width against the space
 * per bar, and how many up and down candles were found.
 *
 *   node e2e/parity/capture.mjs <name>        (writes e2e/artifacts/chart-<name>.png and chart-<name>.json)
 */
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const dashboard = path.resolve(here, "../..");
const artifacts = path.resolve(here, "../artifacts");
const dist = path.join(artifacts, "parity-dist");
const name = process.argv[2] ?? "capture";
const { chromium } = createRequire(path.join(dashboard, "noop.js"))("playwright");

const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2", ".svg": "image/svg+xml" };
const server = http.createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://localhost");
  const file = path.join(dist, url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname));
  if (!file.startsWith(dist) || !fs.existsSync(file)) {
    res.writeHead(404).end();
    return;
  }
  res.writeHead(200, { "Content-Type": types[path.extname(file)] ?? "application/octet-stream" }).end(fs.readFileSync(file));
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const port = server.address().port;

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1200, height: 520 }, deviceScaleFactor: 1, colorScheme: "dark" });
  await page.goto(`http://127.0.0.1:${port}/`);
  await page.waitForTimeout(1500);
  const shot = await page.screenshot({ path: path.join(artifacts, `chart-${name}.png`) });
  // The chart's own picture, the one the PNG export puts in the image, when the page exposes the handle.
  const own = await page.evaluate(() => window.priceChart?.current?.takeScreenshot().toDataURL("image/png") ?? null);
  if (own) fs.writeFileSync(path.join(artifacts, `chart-${name}-screenshot.png`), Buffer.from(own.split(",")[1], "base64"));
  const tokens = await page.evaluate(() => {
    const style = getComputedStyle(document.querySelector(".hk"));
    return { up: style.getPropertyValue("--up").trim(), down: style.getPropertyValue("--down").trim() };
  });
  const report = await page.evaluate(
    async ({ png, tokens }) => {
      const probe = document.createElement("canvas").getContext("2d");
      const hex = (value) => {
        probe.fillStyle = value; // the browser normalizes any CSS color to #rrggbb
        const h = probe.fillStyle;
        return [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
      };
      const up = hex(tokens.up);
      const down = hex(tokens.down);
      const img = new Image();
      img.src = `data:image/png;base64,${png}`;
      await img.decode();
      const canvas = document.createElement("canvas");
      canvas.width = img.width;
      canvas.height = img.height;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(img, 0, 0);
      const { data, width, height } = ctx.getImageData(0, 0, img.width, img.height);
      const at = (x, y) => [data[(y * width + x) * 4], data[(y * width + x) * 4 + 1], data[(y * width + x) * 4 + 2]];
      const same = (a, b) => a[0] === b[0] && a[1] === b[1] && a[2] === b[2];
      // Columns that hold at least 3 pixels of exactly one candle color; contiguous columns form one candle.
      const runs = [];
      let open = null;
      for (let x = 0; x < width; x += 1) {
        let u = 0;
        let d = 0;
        let top = height;
        let bottom = -1;
        for (let y = 0; y < height; y += 1) {
          const p = at(x, y);
          if (same(p, up)) u += 1;
          else if (same(p, down)) d += 1;
          else continue;
          top = Math.min(top, y);
          bottom = Math.max(bottom, y);
        }
        const kind = u >= 3 && u >= d ? "up" : d >= 3 ? "down" : null;
        if (kind && open && open.kind === kind && open.end === x - 1) {
          open.end = x;
          open.cols.push({ x, n: Math.max(u, d), top, bottom });
        } else if (kind) {
          open = { kind, start: x, end: x, cols: [{ x, n: Math.max(u, d), top, bottom }] };
          runs.push(open);
        } else open = null;
      }
      const candles = runs
        .filter((r) => r.end - r.start >= 1)
        .map((r) => {
          const mid = r.cols.reduce((best, c) => (c.n > best.n ? c : best), r.cols[0]);
          const cx = Math.round((r.start + r.end) / 2);
          // body center: the middle of the longest run of candle color in the center column
          const col = r.cols.find((c) => c.x === cx) ?? mid;
          let best = { from: 0, len: 0 };
          let from = -1;
          for (let y = col.top; y <= col.bottom + 1; y += 1) {
            const p = y <= col.bottom ? at(cx, y) : [0, 0, 0];
            const hit = same(p, r.kind === "up" ? up : down);
            if (hit && from < 0) from = y;
            if (!hit && from >= 0) {
              if (y - from > best.len) best = { from, len: y - from };
              from = -1;
            }
          }
          const cy = best.from + Math.floor(best.len / 2);
          const p = at(cx, cy);
          return { kind: r.kind, x: r.start, bodyWidth: r.end - r.start + 1, center: [cx, cy], color: `#${p.map((v) => v.toString(16).padStart(2, "0")).join("")}` };
        });
      const centers = candles.map((c) => c.center[0]);
      const steps = centers.slice(1).map((c, i) => c - centers[i]);
      const step = steps.length ? steps.slice().sort((a, b) => a - b)[Math.floor(steps.length / 2)] : 0;
      const widths = candles.map((c) => c.bodyWidth).sort((a, b) => a - b);
      const bodyWidth = widths.length ? widths[Math.floor(widths.length / 2)] : 0;
      return {
        tokens,
        found: { up: candles.filter((c) => c.kind === "up").length, down: candles.filter((c) => c.kind === "down").length },
        medianStep: step,
        medianBodyWidth: bodyWidth,
        bodyShare: step ? Number((bodyWidth / step).toFixed(3)) : null,
        samples: [...candles.filter((c) => c.kind === "up" && c.bodyWidth >= 4).slice(0, 3), ...candles.filter((c) => c.kind === "down" && c.bodyWidth >= 4).slice(0, 3)],
      };
    },
    { png: shot.toString("base64"), tokens },
  );
  fs.writeFileSync(path.join(artifacts, `chart-${name}.json`), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify(report, null, 2));
} finally {
  await browser.close();
  server.close();
}
