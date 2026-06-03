"""Unit tests for shared.config_loader.load_pipeline_config.

Locks in the path-anchoring contract that prevents KB output from drifting
when workers are launched from different working directories.

Run: python -m tests.test_config_loader
"""

import os
import tempfile
from pathlib import Path

import yaml

from shared.config_loader import load_pipeline_config


def _write(dir_: Path, name: str, data: dict) -> Path:
    p = dir_ / name
    p.write_text(yaml.safe_dump(data))
    return p


def test_relative_kb_root_anchored_to_config_dir():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d).resolve()
        cfg_path = _write(d, "pipeline_config.yaml", {
            "output": {"kb_root": "./kb"},
        })
        # CWD must NOT affect the result — chdir somewhere random
        old_cwd = os.getcwd()
        try:
            os.chdir("/")
            cfg = load_pipeline_config(cfg_path)
        finally:
            os.chdir(old_cwd)
        assert cfg["output"]["kb_root"] == str(d / "kb")
        assert Path(cfg["output"]["kb_root"]).is_absolute()


def test_absolute_kb_root_passthrough():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d).resolve()
        cfg_path = _write(d, "pipeline_config.yaml", {
            "output": {"kb_root": "/var/lib/kb"},
        })
        cfg = load_pipeline_config(cfg_path)
        assert cfg["output"]["kb_root"] == "/var/lib/kb"


def test_storage_local_root_anchored_too():
    """The S3 scaffold's storage.local.root field is also anchored.

    Not currently read by activities, but resolving it here means the future
    storage.create_store() cutover won't need to repeat this logic.
    """
    with tempfile.TemporaryDirectory() as d:
        d = Path(d).resolve()
        cfg_path = _write(d, "pipeline_config.yaml", {
            "storage": {"backend": "local", "local": {"root": "./kb"}},
        })
        cfg = load_pipeline_config(cfg_path)
        assert cfg["storage"]["local"]["root"] == str(d / "kb")


def test_missing_fields_tolerated():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d).resolve()
        cfg_path = _write(d, "pipeline_config.yaml", {"unrelated": 42})
        cfg = load_pipeline_config(cfg_path)
        # _config_dir is always set
        assert cfg["_config_dir"] == str(d)
        assert "output" not in cfg
        assert "storage" not in cfg


def test_config_dir_is_absolute_and_resolved():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d).resolve()
        # Pass a relative-looking path; loader should resolve it
        old_cwd = os.getcwd()
        try:
            os.chdir(d)
            cfg_path = _write(d, "pipeline_config.yaml", {"output": {"kb_root": "./kb"}})
            cfg = load_pipeline_config("./pipeline_config.yaml")
        finally:
            os.chdir(old_cwd)
        assert Path(cfg["_config_dir"]).is_absolute()
        assert cfg["_config_dir"] == str(d)
        assert cfg["output"]["kb_root"] == str(d / "kb")


def test_dotdot_paths_resolve_correctly():
    """`../kb` from etl/config/ should resolve to etl/kb (one level up)."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d).resolve()
        config_dir = d / "config"
        config_dir.mkdir()
        cfg_path = _write(config_dir, "pipeline_config.yaml", {
            "output": {"kb_root": "../kb"},
        })
        cfg = load_pipeline_config(cfg_path)
        assert cfg["output"]["kb_root"] == str(d / "kb")


def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    return len(tests)


if __name__ == "__main__":
    n = _run_all()
    print(f"PASS: {n} config_loader tests")
