/**
 * Runs on Vercel before deploy. Reads RAILWAY_BACKEND_URL and patches vercel.json
 * so you don't have to commit your Railway URL to Git.
 *
 * Set in Vercel → Settings → Environment Variables:
 *   RAILWAY_BACKEND_URL = https://your-app.up.railway.app
 */
const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..");
const vercelPath = path.join(root, "vercel.json");
const backendUrl = (process.env.RAILWAY_BACKEND_URL || "").replace(/\/$/, "");

if (!backendUrl) {
  console.log(
    "RAILWAY_BACKEND_URL not set — using vercel.json as-is (edit the placeholder URL manually if needed)."
  );
  process.exit(0);
}

const vercel = JSON.parse(fs.readFileSync(vercelPath, "utf8"));
const apiRewrite = vercel.rewrites.find((r) => r.source && r.source.includes("/api/"));
if (apiRewrite) {
  apiRewrite.destination = `${backendUrl}/api/$1`;
  console.log(`Patched API rewrite → ${apiRewrite.destination}`);
}
fs.writeFileSync(vercelPath, JSON.stringify(vercel, null, 2) + "\n");

const envJsPath = path.join(root, "frontend", "js", "env-config.js");
const apiBase = process.env.VITE_API_BASE || "";
fs.writeFileSync(
  envJsPath,
  `// Auto-generated at build — do not commit (gitignored)\nwindow.__API_BASE__ = ${JSON.stringify(apiBase)};\n`
);
console.log(`Wrote env-config.js (API_BASE=${apiBase || "(empty — use /api rewrites)"})`);
