const express = require("express");
const path = require("path");
const fs = require("fs");

const PROJECT_ROOT = path.resolve(__dirname, "..");
const KB_DIR = path.join(PROJECT_ROOT, "etl", "kb");

const app = express();

app.use(express.static(path.join(__dirname, "public")));
app.use("/kb", express.static(KB_DIR));

app.get("/api/papers", (_req, res) => {
  if (!fs.existsSync(KB_DIR)) return res.json([]);
  const papers = fs
    .readdirSync(KB_DIR, { withFileTypes: true })
    .filter((d) => d.isDirectory() && fs.existsSync(path.join(KB_DIR, d.name, "tree.json")))
    .map((d) => d.name)
    .sort();
  res.json(papers);
});

// Stream the source PDF referenced by a paper's tree.json (pdf_path is absolute).
// Sandboxed to PROJECT_ROOT to prevent traversal.
app.get("/pdf/:paper", (req, res) => {
  const treePath = path.join(KB_DIR, req.params.paper, "tree.json");
  if (!fs.existsSync(treePath)) return res.sendStatus(404);
  const { pdf_path } = JSON.parse(fs.readFileSync(treePath, "utf8"));
  if (!pdf_path) return res.sendStatus(404);
  const resolved = path.resolve(pdf_path);
  if (!resolved.startsWith(PROJECT_ROOT + path.sep)) return res.sendStatus(403);
  if (!fs.existsSync(resolved)) return res.sendStatus(404);
  res.type("application/pdf").sendFile(resolved);
});

const PORT = process.env.PORT || 5173;
app.listen(PORT, () => console.log(`kb viewer  ->  http://localhost:${PORT}`));
