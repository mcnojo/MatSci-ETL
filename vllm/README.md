# vLLM OCR benchmark utility

Standalone probes for the terraform-managed chandra vLLM endpoint. This
folder is separate from the pipeline path (`shared/vllm/*`, `pipeline/*`)
and only exists for hand-benchmarking a running box.

## Operator-side launch

The vLLM box itself is terraform-managed. Workers reach it via EC2 tag
lookup (`vllm_role_<role_key>=true`). Bring it up as part of either motif:

```bash
bin/batch/up.sh           # or bin/live/up.sh — both include shared/vllm
```

See `bin/README.md` for the operator manual.

## Client (benchmark utility)

`vllm/client.py` sends a single image to the chandra endpoint. Useful for
sanity-checking a running box without going through the pipeline. Pass
`--host` as the public IP of the vLLM box (look it up via terraform output
or EC2 tags; the pipeline itself resolves via `shared/vllm/resolve.py`).

```bash
python vllm/client.py image.png --host <vllm-public-ip>
python vllm/client.py image.png --task ocr --host <vllm-public-ip>
python vllm/client.py image.png --host <vllm-public-ip> \
    --prompt "Extract tables as markdown"
```

Results save to `vllm/results/`.

`vllm/batch.py` runs `client.ocr_image()` over a hand-listed batch of
image paths under `../data/pages/`. Update `IMAGES` in the file to point at
whatever set you're benchmarking.

`vllm/compare.py` renders a side-by-side HTML page from anything in
`vllm/results/` — original figure vs OCR output, keyed by filename.

## Logs (terraform-managed box)

```bash
aws ssm start-session --target <vllm-instance-id>
tail -f /var/log/vllm.log
```
