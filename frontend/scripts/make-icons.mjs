// Generates PWA icons (PNG) without external dependencies: SDF rasterisation + zlib PNG encoding.
import { writeFileSync, mkdirSync } from "node:fs";
import { deflateSync } from "node:zlib";

const OUT = new URL("../public/icons/", import.meta.url);
mkdirSync(OUT, { recursive: true });

const crcTable = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
const crc32 = (buf) => {
  let c = 0xffffffff;
  for (const b of buf) c = crcTable[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
};
const chunk = (type, data) => {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const td = Buffer.concat([Buffer.from(type), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(td));
  return Buffer.concat([len, td, crc]);
};
function png(w, h, rgba) {
  const raw = Buffer.alloc((w * 4 + 1) * h);
  for (let y = 0; y < h; y++) {
    raw[y * (w * 4 + 1)] = 0;
    rgba.copy(raw, y * (w * 4 + 1) + 1, y * w * 4, (y + 1) * w * 4);
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; ihdr[9] = 6; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", ihdr), chunk("IDAT", deflateSync(raw, { level: 9 })), chunk("IEND", Buffer.alloc(0))]);
}

// Signed distance helpers (unit square coordinates, centre 0.5,0.5)
const hexSD = (x, y, r) => {
  const kx = -0.866025404, ky = 0.5, kz = 0.577350269;
  let px = Math.abs(y), py = Math.abs(x); // pointy-top hex
  const d = 2 * Math.min(kx * px + ky * py, 0);
  px -= d * kx; py -= d * ky;
  px -= Math.min(Math.max(px, -kz * r), kz * r); py -= r;
  return Math.hypot(px, py) * Math.sign(py);
};
const mix = (a, b, t) => a + (b - a) * t;
const clamp = (v) => Math.max(0, Math.min(1, v));

function render(size, { scale = 1, rounded = 0.22, bleed = false }) {
  const buf = Buffer.alloc(size * size * 4);
  const SS = 3;
  const bg = [7, 11, 20];
  for (let py = 0; py < size; py++) {
    for (let px = 0; px < size; px++) {
      let acc = [0, 0, 0, 0];
      for (let sy = 0; sy < SS; sy++) for (let sx = 0; sx < SS; sx++) {
        const u = (px + (sx + 0.5) / SS) / size, v = (py + (sy + 0.5) / SS) / size;
        // rounded-square mask
        let inside = 1;
        if (!bleed) {
          const qx = Math.abs(u - 0.5) - (0.5 - rounded), qy = Math.abs(v - 0.5) - (0.5 - rounded);
          const d = Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) + Math.min(Math.max(qx, qy), 0) - rounded;
          inside = d <= 0 ? 1 : 0;
        }
        if (!inside) continue;
        const x = (u - 0.5) / scale, y = (v - 0.5) / scale;
        // background radial glow
        const g = clamp(1 - Math.hypot(x, y + 0.05) / 0.62);
        let c = [mix(bg[0], 14, g), mix(bg[1], 32, g), mix(bg[2], 58, g)];
        // hex ring
        const ring = Math.abs(hexSD(x, y, 0.3)) - 0.022;
        const ringA = clamp(1 - ring / 0.012);
        const glow = clamp(1 - Math.abs(hexSD(x, y, 0.3)) / 0.09) * 0.35;
        const t = clamp((y + 0.3) / 0.6);
        const ringCol = [mix(34, 139, t), mix(211, 92, t), mix(238, 246, t)];
        c = c.map((ch, i) => mix(ch, ringCol[i], Math.max(ringA, glow)));
        // swarm nodes: centre + 6 around
        const nodes = [[0, 0, 0.055]];
        for (let k = 0; k < 6; k++) nodes.push([Math.cos(k * Math.PI / 3) * 0.15, Math.sin(k * Math.PI / 3) * 0.15, 0.03]);
        for (const [nx, ny, nr] of nodes) {
          const dn = Math.hypot(x - nx, y - ny) - nr;
          const a = clamp(1 - dn / 0.01);
          const col = nr > 0.04 ? [251, 191, 36] : [103, 232, 249];
          c = c.map((ch, i) => mix(ch, col[i], a));
        }
        acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]; acc[3] += 255;
      }
      const n = SS * SS, o = (py * size + px) * 4;
      buf[o] = acc[0] / n; buf[o + 1] = acc[1] / n; buf[o + 2] = acc[2] / n; buf[o + 3] = acc[3] / n;
    }
  }
  return png(size, size, buf);
}

writeFileSync(new URL("icon-192.png", OUT), render(192, {}));
writeFileSync(new URL("icon-512.png", OUT), render(512, {}));
writeFileSync(new URL("icon-maskable-512.png", OUT), render(512, { scale: 0.72, bleed: true }));
writeFileSync(new URL("apple-touch-icon.png", OUT), render(180, { bleed: true }));
writeFileSync(new URL("favicon-32.png", OUT), render(32, {}));
writeFileSync(new URL("favicon.svg", OUT), `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#8b5cf6"/></linearGradient></defs><rect width="64" height="64" rx="14" fill="#070b14"/><polygon points="32,12.8 48.6,22.4 48.6,41.6 32,51.2 15.4,41.6 15.4,22.4" fill="none" stroke="url(#g)" stroke-width="3"/><circle cx="32" cy="32" r="3.6" fill="#fbbf24"/>${[0,1,2,3,4,5].map(k=>`<circle cx="${(32+Math.cos(k*Math.PI/3)*9.6).toFixed(2)}" cy="${(32+Math.sin(k*Math.PI/3)*9.6).toFixed(2)}" r="2" fill="#67e8f9"/>`).join("")}</svg>`);
console.log("icons written");
