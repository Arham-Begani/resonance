// WCAG 2.x contrast check for the design tokens in src/styles/tokens.css.
// Run: node scripts/contrast.mjs   (exits 1 if any required pair fails)
// Text pairs need 4.5:1; UI boundaries, focus rings, and chart marks need 3:1.
// Translucent tints (badges, callouts, selected rows) are alpha-composited over the surfaces
// they actually sit on before measuring.

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

// rgba tints from tokens.css: [base color, alpha]
const tints = {
  accentSubtle: ["#65C5D4", 0.12],
  successSubtle: ["#6CCB94", 0.12],
  warningSubtle: ["#E3B65E", 0.12],
  dangerSubtle: ["#F28B82", 0.12],
};

function rgb(hex) {
  const n = hex.replace("#", "");
  return [0, 2, 4].map((i) => parseInt(n.slice(i, i + 2), 16));
}

function hexOf([r, g, b]) {
  return `#${[r, g, b].map((v) => Math.round(v).toString(16).padStart(2, "0")).join("")}`;
}

function over(tint, base) {
  const [c, a] = tints[tint];
  const fg = rgb(c);
  const bg = rgb(tokens[base]);
  return hexOf(fg.map((v, i) => v * a + bg[i] * (1 - a)));
}

function luminance(hex) {
  const lin = (c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  const [r, g, b] = rgb(hex).map((v) => lin(v / 255));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function ratio(a, b) {
  const [l1, l2] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (l1 + 0.05) / (l2 + 0.05);
}

const surfaces = ["bg", "surface", "raised", "hover"];
const checks = [
  ...["text", "textSecondary", "textTertiary", "accent", "success", "warning", "danger"].flatMap((fg) =>
    surfaces.map((bg) => [fg, tokens[fg], bg, tokens[bg], 4.5]),
  ),
  ["onAccent", tokens.onAccent, "accent", tokens.accent, 4.5],
  ["onAccent", tokens.onAccent, "accentStrong", tokens.accentStrong, 4.5],
  ...["borderStrong", "accent", "traceReference"].flatMap((fg) => surfaces.map((bg) => [fg, tokens[fg], bg, tokens[bg], 3])),
  // Semantic badges/callouts: their own text color, and body text, on their tint.
  ...[
    ["accent", "accentSubtle"],
    ["success", "successSubtle"],
    ["warning", "warningSubtle"],
    ["danger", "dangerSubtle"],
  ].flatMap(([fg, tint]) =>
    ["bg", "surface", "raised"].flatMap((base) => [
      [fg, tokens[fg], `${tint}/${base}`, over(tint, base), 4.5],
      ["textSecondary", tokens.textSecondary, `${tint}/${base}`, over(tint, base), 4.5],
      ["text", tokens.text, `${tint}/${base}`, over(tint, base), 4.5],
    ]),
  ),
];

// UI-pass additions (Oct 2026), measured on the surfaces they are used on:
//  - the dashed "prediction" edge and the case-progress / choice-tile edges use border-strong
//    and accent, already required at 3:1 above;
//  - semantic verdict text (tone goal lines) sits on raised tiles: success/danger on raised above;
//  - the filled secondary button's edge (control-border, 20% text over raised) is decorative: the
//    raised fill and the label identify the control. Reported here, not required.
const decorative = [["controlBorder (20%)", "#E8EEF2", 0.2, "raised"], ["borderSoft (12%)", "#E8EEF2", 0.12, "bg"]];
for (const [name, hex, alpha, base] of decorative) {
  const fg = rgb(hex);
  const bg = rgb(tokens[base]);
  const composite = hexOf(fg.map((v, i) => v * alpha + bg[i] * (1 - alpha)));
  console.log(`info ${name.padEnd(20)} on ${base.padEnd(8)} ${ratio(composite, tokens[base]).toFixed(2)}:1 (decorative, no minimum)`);
}

let failed = 0;
for (const [fgName, fg, bgName, bg, min] of checks) {
  const r = ratio(fg, bg);
  const ok = r >= min;
  if (!ok) failed += 1;
  console.log(`${ok ? "ok  " : "FAIL"} ${fgName.padEnd(14)} on ${bgName.padEnd(22)} ${r.toFixed(2)}:1 (min ${min})`);
}
console.log(failed ? `${failed} pair(s) below the minimum` : `all ${checks.length} pairs pass`);
process.exit(failed ? 1 : 0);
