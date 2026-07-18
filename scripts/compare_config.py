#!/usr/bin/env python3
"""比较 config.yaml 与 config.example.yaml 的键差异。"""

import sys
from pathlib import Path

import yaml


def flatten_keys(d, prefix=""):
    """递归展平嵌套字典的键。"""
    keys = set()
    for k, v in d.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            keys.update(flatten_keys(v, full))
        else:
            keys.add(full)
    return keys


def main():
    root = Path(__file__).parent.parent
    config = yaml.safe_load((root / "configs/config.yaml").read_text(encoding="utf-8"))
    example = yaml.safe_load((root / "configs/config.example.yaml").read_text(encoding="utf-8"))

    config_keys = flatten_keys(config)
    example_keys = flatten_keys(example)

    only_config = config_keys - example_keys
    only_example = example_keys - config_keys

    print("=== Keys only in config.yaml (missing from example) ===")
    for k in sorted(only_config):
        print(f"  {k}")

    print("\n=== Keys only in config.example.yaml (missing from config) ===")
    for k in sorted(only_example):
        print(f"  {k}")

    print(f"\nTotal: config.yaml={len(config_keys)} keys, example={len(example_keys)} keys")
    print(f"Missing from example: {len(only_config)}")
    print(f"Missing from config: {len(only_example)}")

    if only_config or only_example:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
