const express = require("express");
const path = require("path");
const fs = require("fs");

const PROJECT_ROOT = path.resolve(__dirname, "..");
const TREES_DIR = path.join(PROJECT_ROOT, "trees");
const PDF_DIR = path.join(PROJECT_ROOT, "ether_papers");
const ANNOTATIONS_DIR = path.join(PROJECT_ROOT, "benchmark_annotations");

// deterministic name normaliser: lowercase, [space _] -> -, keep [a-z0-9-.]
// used to bridge tree.paper_id (already slugged) to ether_papers/*.pdf which
// carry original casing/spaces/underscores.
const slug = (s) => s.toLowerCase().replace(/[\s_]+/g, "-").replace(/[^a-z0-9.\-]/g, "");

function buildPdfIndex() {
  if (!fs.existsSync(PDF_DIR)) return {};
  const idx = {};
  for (const f of fs.readdirSync(PDF_DIR)) {
    if (!f.toLowerCase().endsWith(".pdf")) continue;
    const key = slug(f.slice(0, -4));
    if (idx[key]) throw new Error(`pdf slug collision: ${f} vs ${idx[key]}`);
    idx[key] = f;
  }
  return idx;
}

const PDF_INDEX = buildPdfIndex();

fs.mkdirSync(ANNOTATIONS_DIR, { recursive: true });

const app = express();
app.use(express.json({ limit: "2mb" }));
app.use(express.static(path.join(__dirname, "public")));
app.use("/trees", express.static(TREES_DIR));

app.get("/api/papers", (_req, res) => {
  if (!fs.existsSync(TREES_DIR)) return res.json([]);
  const papers = fs
    .readdirSync(TREES_DIR, { withFileTypes: true })
    .filter((d) => d.isDirectory() && fs.existsSync(path.join(TREES_DIR, d.name, "tree.json")))
    .map((d) => d.name)
    .sort();
  res.json(papers);
});

// paper_id -> local PDF via slug-index. tree.json's pdf_path is an s3 URI so
// we ignore it and match on paper_id alone.
app.get("/pdf/:paper", (req, res) => {
  const filename = PDF_INDEX[slug(req.params.paper)];
  if (!filename) return res.sendStatus(404);
  res.type("application/pdf").sendFile(path.join(PDF_DIR, filename));
});

// annotations: JSON blob per paper. Read-or-empty, atomic overwrite on write.
const annotationsPath = (paper) => path.join(ANNOTATIONS_DIR, `${slug(paper)}.json`);

app.get("/api/annotations/:paper", (req, res) => {
  const p = annotationsPath(req.params.paper);
  if (!fs.existsSync(p)) return res.json(null);
  res.type("application/json").send(fs.readFileSync(p, "utf8"));
});

app.put("/api/annotations/:paper", (req, res) => {
  const p = annotationsPath(req.params.paper);
  const tmp = p + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(req.body, null, 2));
  fs.renameSync(tmp, p);
  res.json({ saved: true, path: path.relative(PROJECT_ROOT, p) });
});

const PORT = process.env.PORT || 5173;
app.listen(PORT, () => console.log(`kb viewer  ->  http://localhost:${PORT}`));
