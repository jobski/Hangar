"""Just enough of the GGUF format to size a model: its architecture numbers, read from the file's header.

The KV cache is what grows with context: per token it holds a key and a value for every KV head in every layer, so
bytes/token = layers x kv_heads x (key_dim + value_dim) x bytes per element of the cache type. With that and the file
size, Hangar can say how much memory a context length costs and pick one that fits.
"""
from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path

_SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
STRING, ARRAY = 8, 9
WANT = ("block_count", "context_length", "embedding_length", "attention.head_count", "attention.head_count_kv",
        "attention.key_length", "attention.value_length", "attention.sliding_window", "full_attention_interval")
# bytes per element of llama.cpp's KV cache types (q8_0/q4_0 blocks carry a scale per 32 values)
CACHE_BYTES = {"f32": 4.0, "f16": 2.0, "bf16": 2.0, "q8_0": 34 / 32, "q5_1": 24 / 32, "q5_0": 22 / 32,
               "q4_1": 20 / 32, "q4_0": 18 / 32, "iq4_nl": 18 / 32}


class _Reader:
    def __init__(self, f):
        self.f = f

    def take(self, fmt):
        n = struct.calcsize(fmt)
        b = self.f.read(n)
        if len(b) != n:
            raise ValueError("truncated GGUF header")
        return struct.unpack(fmt, b)[0]

    def string(self) -> str:
        n = self.take("<Q")
        if n > 1 << 24:
            raise ValueError("implausible string length")
        return self.f.read(n).decode("utf-8", "replace")

    def value(self, t, keep: bool):
        if t in _SCALAR:
            return self.take(_SCALAR[t])
        if t == STRING:
            if not keep:
                n = self.take("<Q")
                if n > 1 << 24:
                    raise ValueError("implausible string length")
                self.f.seek(n, 1)
                return None
            return self.string()
        if t == ARRAY:
            et, n = self.take("<I"), self.take("<Q")
            if et in _SCALAR and not keep:          # e.g. token scores: skip without unpacking
                self.f.seek(n * struct.calcsize(_SCALAR[et]), 1)
                return None
            items = [self.value(et, keep) for _ in range(n)]
            return items if keep else None
        raise ValueError(f"unknown GGUF value type {t}")


@lru_cache(maxsize=256)
def _read(path: str, mtime: float) -> dict:
    out: dict = {}
    with open(path, "rb") as f:
        r = _Reader(f)
        if f.read(4) != b"GGUF":
            raise ValueError(f"{path} is not a GGUF file")
        version = r.take("<I")
        if version < 2:
            raise ValueError("GGUF v1 is not supported")
        r.take("<Q")                                   # tensor count
        kv_count = r.take("<Q")
        arch = None
        for _ in range(kv_count):
            key = r.string()
            t = r.take("<I")
            short = key.split(".", 1)[1] if arch and key.startswith(arch + ".") else None
            keep = key in ("general.architecture", "general.name") or short in WANT
            v = r.value(t, keep)
            if key == "general.architecture":
                arch = v
                out["arch"] = v
            elif key == "general.name":
                out["name"] = v
            elif short in WANT:
                out[short] = v
    return out


def metadata(path: str | Path) -> dict:
    """Architecture numbers from a GGUF (the first shard of a split model); {} if it cannot be read."""
    p = Path(path)
    try:
        return dict(_read(str(p), p.stat().st_mtime))
    except (OSError, ValueError, struct.error):
        return {}


def kv_bytes_per_token(meta: dict, cache_type_k: str = "f16", cache_type_v: str | None = None) -> float | None:
    """Memory one token of context costs in the KV cache; None when the header lacks the numbers."""
    layers = meta.get("block_count")
    heads = meta.get("attention.head_count")
    if not layers or not heads:
        return None
    kv = meta.get("attention.head_count_kv", heads)
    emb = meta.get("embedding_length") or 0
    head_dim = emb // (heads if isinstance(heads, int) else max(heads)) if emb else 128
    kd, vd = meta.get("attention.key_length") or head_dim, meta.get("attention.value_length") or head_dim
    bk = CACHE_BYTES.get(cache_type_k, 2.0)
    bv = CACHE_BYTES.get(cache_type_v or cache_type_k, 2.0)
    per_layer_kv = kv if isinstance(kv, list) else [kv] * layers
    # hybrid models (attention every Nth layer, the rest recurrent) keep a cache only in their attention layers
    interval = meta.get("full_attention_interval")
    if isinstance(interval, int) and interval > 1:
        per_layer_kv = [h for i, h in enumerate(per_layer_kv) if (i + 1) % interval == 0]
    return float(sum(h * (kd * bk + vd * bv) for h in per_layer_kv if h))


def max_context(meta: dict) -> int | None:
    v = meta.get("context_length")
    return int(v) if isinstance(v, int) and v > 0 else None
