import asyncio
import argparse
import json
import os
import hashlib
import struct
from pathlib import Path
from typing import Dict, Set, List

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
    
def make_meta_for_file(filename: str, block_size: int):
    p = Path(filename)
    filesize = p.stat().st_size
    total_blocks = (filesize + block_size - 1)//block_size
    sha = sha256_file(p)
    return {
        "filename": p.name,
        "filesize": filesize,
        "block_size": block_size,
        "total_blocks": total_blocks,
        "sha256": sha
    }

def seed_prepare(filename: str, block_size: int, out_meta: str, peer_storage: str, peer_id: str):
    meta = make_meta_for_file(filename, block_size)
    with open(out_meta,"w") as f:
        json.dump(meta, f, indent=2)
    # split blocks to storage
    storage = Path(peer_storage) / peer_id
    storage.mkdir(parents=True, exist_ok=True)
    with open(filename,"rb") as fin:
        for i in range(meta["total_blocks"]):
            chunk = fin.read(block_size)
            (storage / f"block_{i:08d}.bin").write_bytes(chunk)
    print("Seeder prepared:", out_meta)

