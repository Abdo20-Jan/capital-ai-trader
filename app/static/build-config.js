// Usado só no Vercel: grava o endereço do backend (variável API_URL) em config.js.
const fs = require("fs");
const url = (process.env.API_URL || "").replace(/\/$/, "");
if (!url) { console.error("Defina a variável API_URL no Vercel (ex.: https://seu-app.up.railway.app)"); process.exit(1); }
fs.writeFileSync(__dirname + "/config.js", `window.API_BASE = ${JSON.stringify(url)};\n`);
console.log("config.js →", url);
