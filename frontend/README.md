# KB viewer

Side-by-side PDF / doc-tree viewer for `pipeline/kb/<paper>/`.

```
cd frontend
npm install
npm start             # http://localhost:5173
PORT=3000 npm start   # custom port
```

Reads from `../pipeline/kb/<paper>/{tree.json,assets/...}` and streams the PDF
referenced in `tree.json:pdf_path` (sandboxed to project root).

URL `?paper=<id>` jumps to a specific paper.
