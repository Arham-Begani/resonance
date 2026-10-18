// WCAG 2.x contrast check for the design tokens in src/styles/tokens.css.
// Run: node scripts/contrast.mjs   (exits 1 if any required pair fails)
// Text pairs need 4.5:1; UI boundaries, focus rings, and chart marks need 3:1.

const tokens = {
  bg: "#0B1014",
  surface: "#121A20",
  raised: "#1A252D",
  hover: "#22303A",
  text: "#E8EEF2",
  textSecondary: "#A7B8C3",
  textTertiary: "#8B9DA9",
  accent: "#65C5D4",
  accentStrong: "#86D3DF",
  onAccent: "#0B1014",
  borderStrong: "#6A7F8C",
  traceReference: "#9AABB6",
  success: "#6CCB94",
  warning: "#E3B65E",
  danger: "#F28B82",
};

function luminance(hex) {
  const n = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(n.slice(i, i + 2), 16) / 255);
  const lin = (c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function ratio(a, b) {
  const [l1, l2] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (l1 + 0.05) / (l2 + 0.05);
}

const surfaces = ["bg", "surface", "raised", "hover"];
const checks = [
  ...["text", "textSecondary", "textTertiary", "accent", "success", "warning", "danger"].flatMap(
    (fg) => surfaces.map((bg) => [fg, bg, 4.5]),
  ),
  ["onAccent", "accent", 4.5],
  ["onAccent", "accentStrong", 4.5],
  ...["borderStrong", "accent", "traceReference"].flatMap((fg) =>
    surfaces.map((bg) => [fg, bg, 3]),
  ),
];

let failed = 0;
for (const [fg, bg, min] of checks) {
  const r = ratio(tokens[fg], tokens[bg]);
  const ok = r >= min;
  if (!ok) failed += 1;
  console.log(`${ok ? "ok  " : "FAIL"} ${fg.padEnd(15)} on ${bg.padEnd(8)} ${r.toFixed(2)}:1 (min ${min})`);
}
process.exit(failed ? 1 : 0);
