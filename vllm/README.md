# vLLM OCR Serving

Serve chandra-ocr, DeepSeek-OCR-2, dots.mocr, and olmOCR on EC2 via vLLM.

## Operator-side launch

The vLLM box itself is terraform-managed. For the production / prod-tagged
box that workers reach via EC2 tag lookup, use:

```bash
bin/batch/up.sh           # or bin/live/up.sh — both bring up env_tag=prod
```

For the hybrid local-dev escape hatch (Mac drives `etl/cli.py` against a
dev-tagged AWS vLLM, no Temporal):

```bash
bin/dev/up_vllm.sh
bin/dev/down_vllm.sh
```

See `bin/README.md` for the operator manual.

## Client (benchmark utility)

`vllm/client.py` is a standalone benchmark probe — useful for sanity-checking
a running vLLM endpoint without going through the pipeline. Pass `--host` as
the public IP of the vLLM box (look it up via terraform output or EC2 tags;
the pipeline itself resolves via `shared/vllm/resolve.py`).

```bash
# all models for an image
python vllm/client.py image.png --all --host <vllm-public-ip>

# single model
python vllm/client.py image.png --model chandra --host <vllm-public-ip>

# custom prompt
python vllm/client.py image.png --model dots --host <vllm-public-ip> \
    --prompt "Extract tables as markdown"
```

Results save to `vllm/results/`.

## Box-side serve scripts

`vllm/serve_*.sh` are standalone wrappers around `vllm serve` for manually
benchmarking a single model on a vLLM box (e.g. via SSM `start-session`).
They are not invoked by terraform — `shared/vllm`'s user-data wires two
systemd units (vision + tree_llm) pinned to their respective `hf_model_id`s.

| Script                | Model                  | Port |
| --------------------- | ---------------------- | ---- |
| `serve_deepseek_ocr.sh` | DeepSeek-OCR-2 (3B)   | 8001 |
| `serve_dots_mocr.sh`    | dots.mocr (3B)        | 8002 |
| `serve_olmocr.sh`       | olmOCR-2-7B (FP8)     | 8003 |

## Logs (terraform-managed box)

```bash
aws ssm start-session --target <vllm-instance-id>
tail -f /var/log/vllm_serve.log
```
