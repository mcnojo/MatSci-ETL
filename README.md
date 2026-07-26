# Material Science ETL Pipeline

A pipeline for pulling structure out of dense chemistry and materials science PDFs (in our case sodium-ion battery electrolyte literature).  It takes a raw PDF, rebuilds or infers the section hierarchy, pulls out the figures/tables/equations, runs vision OCR on each one, and drops the result into a hybrid vector index you can query.

## What it does

- **Tree construction.** 
  - PyMuPDF extracts page text, and an LLM finds or derives a table on contents/section structure.  A verifier samples entries to check the result.
- **Visual element extraction.** 
  - Doclayout-yolo processes each page (in the future will try end to end Chandra), and the pipeline crops each element, storing intermediately in S3.
- **Per-element OCR + enrichment.**  
  - Chandra-ocr-2 returns `layout_html` or `figure_analysis` based on the image content. A parser converts both formats into JSON. We also run the chemical-entity regex and the table structure extractor.
- **Figure-aware resummarization.**  
  - We do a second summary pass that uses the enriched element text, allowing us to integrate Figure data from our OCR model, not from raw (often very messy) PyMuPDF OCR.
- **Storage for Agent.**  
  - Aside from tree construction, we write chunks to Qdrant, with each chunk gets a dense vector via bge-m3 and a sparse vector via fastembed BM25.  Later used as Hybrid RAG via RRF for agent recall.

## Architecture

Two lanes share the same `ProcessPdfWorkflow`, one for for live, 'always on' activity, and another for batch jobs:

Live lane

Batch lane


| Lane          | Trigger         | Optimizes for   |
| ------------- | --------------- | --------------- |
| `prod/live/`  | SQS (always-on) | per-PDF latency |
| `prod/batch/` | `cli submit`    | GPU utilization |


Pipeline logic lives in `pipeline/`; `prod/` is just deployment glue. Shared Temporal stuff (task queues, retry policies, client, activity I/O models) is under `shared/temporal/`. Long-lived SecureString slots (API keys, Qdrant creds) sit in `shared/platform/` so tearing compute down at night doesn't wipe them.

## Errata...

- [ParseBench leaderboard](https://huggingface.co/datasets/llamaindex/ParseBench?eval_result=infly/Infinity-Parser2-Pro&leaderboard_task_id=chart)
- [Materials Project APIs/tools](https://docs.materialsproject.org/)
- [pymatgen](https://pymatgen.org/)
- Framework which sits at top of the [chembench leaderboard](https://huggingface.co/spaces/jablonkagroup/ChemBench-Leaderboard), [Nexus Sci Agent](https://github.com/CASIA-LM/S1-NexusAgent)
- [Hackathon submissions](https://llmhackathon.github.io/submissions/) for chem, goldmine of architecures to integrate

## TODO:

- [ ] Add CLI replication instructions
- [ ] Wire optional vLLM ASG for massive batches
- [ ] Benchmark different local models, SOTA models, and try 'all Chandra' OCR.