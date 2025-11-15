#!/usr/bin/env python3
"""
peer.py - Peer P2P threaded

Uso:
  python3 peer.py --peer-id 1 --ip 127.0.0.1 --port 9001 --neighbors neighbors.json --meta file.meta.json --storage ./blocks --recon-dir ./recon

neighbors.json format (JSON list):
[
  {"peer_id": 2, "ip": "127.0.0.1", "port": 9002},
  {"peer_id": 3, "ip": "127.0.0.1", "port": 9003}
]

meta.json format:
{
  "filename": "FileA.bin",
  "filesize": 10240,
  "block_size": 1024,
  "total_blocks": 10,
  "sha256": "..."
}
"""


import socket
import threading
import json
import time
import hashlib
import os
import random
import argparse
from pathlib import Path
from typing import Dict, Set, Tuple, List

LOCK = threading.Lock()

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def recv_json_line(conn: socket.socket, timeout: float = 6.0):
    """Recebe até a primeira linha terminada em \\n e parseia JSON."""
    conn.settimeout(timeout)
    data = b""
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in chunk:
                break
    except socket.timeout:
        pass
    if not data:
        return None
    # take up to first newline
    idx = data.find(b"\n")
    if idx != -1:
        line = data[:idx].decode(errors="ignore")
    else:
        line = data.decode(errors="ignore")
    try:
        return json.loads(line)
    except:
        return None

def send_json_line(conn: socket.socket, obj: dict):
    s = (json.dumps(obj) + "\n").encode()
    conn.sendall(s)

class Peer:
    def __init__(self,
                 peer_id: int,
                 ip: str,
                 port: int,
                 neighbors_json: str,
                 meta_json: str = None,
                 storage_dir: str = "./blocks",
                 recon_dir: str = "./recon"):
        self.peer_id = peer_id
        self.ip = ip
        self.port = port
        self.neighbors: List[Tuple[int,str,int]] = self.load_neighbors(neighbors_json)

        self.storage_dir = Path(storage_dir)
        self.peer_storage = self.storage_dir / f"peer{self.peer_id}"
        self.peer_storage.mkdir(parents=True, exist_ok=True)

        self.recon_dir = Path(recon_dir) / f"peer{self.peer_id}"
        self.recon_dir.mkdir(parents=True, exist_ok=True)

        # metadata
        self.file_name = None
        self.filesize = 0
        self.block_size = 0
        self.total_blocks = 0
        self.file_sha256 = None

        # state
        self.blocks: Dict[int, bytes] = {}     # index -> bytes (in-memory minimal)
        self.have: Set[int] = set()            # indices we have
        self.serving_now: Set[int] = set()     # indices being currently served (to enforce busy)
        self.pending_requests: Set[int] = set()# indices we are currently downloading
        self.pending_lock = threading.Lock()

        # neighbor knowledge: (ip,port) -> set(indices they have)
        self.neighbors_known_blocks: Dict[Tuple[str,int], Set[int]] = {}
        for _, ip_n, port_n in self.neighbors:
            self.neighbors_known_blocks[(ip_n, port_n)] = set()

        # load meta (if provided)
        if meta_json:
            self.load_meta(meta_json)

        # load any already-saved blocks in storage
        self.scan_storage_for_blocks()

        self.stop_event = threading.Event()
        print(f"[{self.peer_id}] Initialized. Neighbors: {len(self.neighbors)}. Have {len(self.have)} blocks.")

    def load_neighbors(self, json_file: str) -> List[Tuple[int,str,int]]:
        with open(json_file, "r") as f:
            arr = json.load(f)
        result = []
        for e in arr:
            result.append((e["peer_id"], e["ip"], e["port"]))
        return result

    def load_meta(self, meta_json: str):
        with open(meta_json, "r") as f:
            m = json.load(f)
        self.file_name = m["filename"]
        self.filesize = m["filesize"]
        self.block_size = m["block_size"]
        self.total_blocks = m["total_blocks"]
        self.file_sha256 = m.get("sha256")
        print(f"[{self.peer_id}] Meta loaded: {self.file_name}, blocks={self.total_blocks}, block_size={self.block_size}")

    def scan_storage_for_blocks(self):
        # look for files named block_<index>.bin
        for p in self.peer_storage.glob("block_*.bin"):
            name = p.name
            try:
                idx = int(name.split("_")[1].split(".")[0])
            except:
                continue
            self.have.add(idx)
            with p.open("rb") as f:
                self.blocks[idx] = f.read()
        print(f"[{self.peer_id}] Scanned storage: found {len(self.have)} block files.")

    # ----------------- networking helpers -----------------
    def handle_client(self, conn: socket.socket, addr):
        try:
            msg = recv_json_line(conn)
            if not msg:
                conn.close()
                return
            mtype = msg.get("type")
            if mtype == "REQ_HAVE":
                # Respond with list of blocks we have
                send_json_line(conn, {"type":"RES_HAVE", "peer_id": self.peer_id, "blocks": sorted(list(self.have))})
                conn.close()
                print(f"[{self.peer_id}] Respose to {msg.get("peer_id")} - Type {mtype} - blocks {self.have} ")
                return

            if mtype == "REQ_BLOCK":
                index = int(msg.get("index"))       
                with LOCK:
                    if index in self.serving_now or index not in self.have:
                        send_json_line(conn, {"type":"RESP_BLOCK", "index": index, "ok": False, "reason": "busy_or_no_block"})
                        conn.close()
                        print(f"[{self.peer_id}] : type [RESP_BLOCK] - INDEX [{index}] - Failed to respond")
                        return
                    # mark as serving
                    self.serving_now.add(index)
                # read bytes from storage (if not present in memory)
                if index not in self.blocks:
                    p = self.peer_storage / f"block_{index:08d}.bin"
                    try:
                        with p.open("rb") as f:
                            data = f.read()
                    except:
                        data = b""
                else:
                    data = self.blocks[index]
                # send response (data encoded as base64 string to keep JSON safe)
                import base64
                b64 = base64.b64encode(data).decode()
                send_json_line(conn, {"type":"RESP_BLOCK", "index": index, "ok": True, "data": b64})
                with LOCK:
                    if index in self.serving_now:
                        self.serving_now.remove(index)
                conn.close()
                print(f"[{self.peer_id}] : type [RESP_BLOCK] - INDEX [{index}] - ok [True] - Responding to peer ")
                return

            if mtype == "HAVE_UPDATE":
                idx = int(msg.get("index"))
                # addr can be ephemeral; match neighbor by ip if possible
                peer_ip = addr[0]
                # update neighbor known table for entries with that ip (could be multiple ports)
                for (_pid, n_ip, n_port) in self.neighbors:
                    if n_ip == peer_ip:
                        self.neighbors_known_blocks[(n_ip, n_port)].add(idx)
                conn.close()
                return

            # unknown
            send_json_line(conn, {"type":"ERROR", "reason":"unknown_message"})
        except Exception as e:
            # robust logging
            print(f"[{self.peer_id}] handle_client error: {e}")
        finally:
            try:
                conn.close()
            except:
                pass

    def server_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.ip, self.port))
        srv.listen(32)
        print(f"[{self.peer_id}] Listening on {self.ip}:{self.port}")
        while True:
            try:
                conn, addr = srv.accept()
                threading.Thread(target=self.handle_client, args=(conn, addr), daemon=True).start()
            except Exception as e:
                print(f"[{self.peer_id}] accept error: {e}")
                time.sleep(0.1)
        print("Closing Server")
        srv.close()

    def query_neighbor_have(self, ip: str, port: int, timeout=2.0) -> Set[int]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((ip, port))
            send_json_line(s, {"type":"REQ_HAVE", "peer_id" : self.peer_id})
            resp = recv_json_line(s, timeout)
            s.close()
            if resp and resp.get("type") == "RES_HAVE":
                blocks = set(resp.get("blocks", []))
                # update local table
                self.neighbors_known_blocks[(ip, port)] = blocks
                return blocks
            return set()
        except Exception:
            return set()

    def broadcast_have_update(self, index: int):
        msg = {"type":"HAVE_UPDATE", "peer_id": self.peer_id, "index": index}
        for (_pid, ip, port) in self.neighbors:
            # best-effort; do not block
            def _send(ip_, port_, m):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(1.0)
                    s.connect((ip_, port_))
                    send_json_line(s, m)
                    s.close()
                except:
                    pass
            threading.Thread(target=_send, args=(ip, port, msg), daemon=True).start()

    def request_block_from(self, index: int, ip: str, port: int) -> bool:
        # guard pending
        with self.pending_lock:
            if index in self.pending_requests:
                return False
            self.pending_requests.add(index)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(6.0)
            s.connect((ip, port))
            send_json_line(s, { "peer_id" : self.peer_id ,"type":"REQ_BLOCK", "index": index})
            resp = recv_json_line(s, timeout=6.0)
            s.close()
            if not resp:
                return False
            if resp.get("type") != "RESP_BLOCK" or not resp.get("ok", False):
                return False
            import base64
            data = base64.b64decode(resp.get("data", ""))
            # save block to disk
            p = self.peer_storage / f"block_{index:08d}.bin"
            with p.open("wb") as f:
                f.write(data)
            with LOCK:
                self.blocks[index] = data
                self.have.add(index)
            # update known table: we now have it
            self.broadcast_have_update(index)
            print(f"[{self.peer_id}] Downloaded block {index} from {ip}:{port} (have now {len(self.have)}/{self.total_blocks})")
            return True
        except Exception as e:
            print(f"[{self.peer_id}] request_block error idx={index} from {ip}:{port} -> {e}")
            return False
        finally:
            with self.pending_lock:
                if index in self.pending_requests:
                    self.pending_requests.remove(index)

    def try_download_missing(self):
        # Build mapping: block -> list of neighbors (ip,port) that (we think) have it
        mapping: Dict[int, List[Tuple[str,int]]] = {}
        # first use known info
        for (ip, port), blset in self.neighbors_known_blocks.items():
            for b in blset:
                mapping.setdefault(b, []).append((ip, port))
        # if mapping empty for many blocks, query neighbors to refresh
        for (_pid, ip, port) in self.neighbors:
            # query only if we have no data for this neighbor
            key = (ip, port)
            if not self.neighbors_known_blocks.get(key):
                bls = self.query_neighbor_have(ip, port)
                for b in bls:
                    mapping.setdefault(b, []).append((ip, port))
        missing = [i for i in range(self.total_blocks) if i not in self.have]
        if not missing:
            return
        # attempt to download some blocks (limit concurrency)
        for idx in missing:
            if idx not in mapping:
                continue
            # choose random neighbor among those listed
            ip, port = random.choice(mapping[idx])
            # ensure we don't request if already pending
            with self.pending_lock:
                if idx in self.pending_requests:
                    continue
                # we'll request in background
                threading.Thread(target=self.request_block_from, args=(idx, ip, port), daemon=True).start()
            # throttle starting threads slightly
            time.sleep(0.01)

    def reassemble_and_check(self):
        # Called when have all blocks
        out_path = self.recon_dir / self.file_name
        print(f"[{self.peer_id}] All blocks present. Reassembling to {out_path} ...")
        with out_path.open("wb") as fout:
            for i in range(self.total_blocks):
                p = self.peer_storage / f"block_{i:08d}.bin"
                if not p.exists():
                    print(f"[{self.peer_id}] missing part during reassembly: {p}")
                    return False
                with p.open("rb") as pf:
                    fout.write(pf.read())
        sha = sha256_file(out_path)
        print(f"[{self.peer_id}] Reconstructed file SHA-256: {sha}")
        if self.file_sha256:
            if sha == self.file_sha256:
                print(f"[{self.peer_id}] Integrity OK (matches meta).")
            else:
                print(f"[{self.peer_id}] Integrity MISMATCH (meta: {self.file_sha256})")
        return True

    def client_loop(self):
        print(f"[{self.peer_id}] Client loop started.")

        start_time = time.time()       # ⬅️ Início da medição do tempo

        while True:
            missing = [i for i in range(self.total_blocks) if i not in self.have]
            if not missing:
                end_time = time.time()   # ⬅️ Fim da medição
                total_time = end_time - start_time

                # Estatísticas
                blocks_downloaded = len(self.have)
                avg_time = total_time / blocks_downloaded if blocks_downloaded > 0 else 0

                print(f"[{self.peer_id}] Download concluído!")
                print(f"[{self.peer_id}] Tempo total de download: {total_time:.2f} s")
                print(f"[{self.peer_id}] Tempo médio por bloco: {avg_time:.4f} s")

                # monta e verifica o arquivo
                self.reassemble_and_check()
                return

            # tenta baixar blocos faltando
            self.try_download_missing()
            time.sleep(0.4)

    def start(self):
        # server thread
        t_srv = threading.Thread(target=self.server_loop, daemon=True)
        t_srv.start()
        # client thread
        t_cli = threading.Thread(target=self.client_loop, daemon=True)
        t_cli.start()

        while t_srv.is_alive():
                t_srv.join(timeout=1)

# -------------------- main --------------------
if __name__ == "__main__":

    print("AIIIIIIIIIIIIIIIIIIi")
    parser = argparse.ArgumentParser()
    parser.add_argument("--peer-id", type=int, required=True)
    parser.add_argument("--ip", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--neighbors", required=True, help="path to neighbors JSON")
    parser.add_argument("--meta", default=None,required=False, help="path to meta JSON")
    parser.add_argument("--storage", default="./blocks", help="base storage dir; blocks saved under <storage>/peer<id>/")
    parser.add_argument("--recon-dir", default="./recon", help="where to write reconstructed file")
    args = parser.parse_args()

    
    peer = Peer(peer_id=args.peer_id,
                ip=args.ip,
                port=args.port,
                neighbors_json=args.neighbors,
                meta_json=args.meta,
                storage_dir=args.storage,
                recon_dir=args.recon_dir)
    peer.start()
