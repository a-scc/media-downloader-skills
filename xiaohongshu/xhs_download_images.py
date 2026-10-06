import argparse
import json
import os
import struct
from io import BytesIO
from typing import Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import requests


DEFAULT_HOSTS = [
    "https://sns-img-qc.xhscdn.com",
    "https://sns-img-bd.xhscdn.com",
    "https://sns-img-hw.xhscdn.com",
    "https://sns-img-qn.xhscdn.com",
    "https://sns-na-i1.xhscdn.com",
    "https://sns-na-i2.xhscdn.com",
    "https://ci.xiaohongshu.com",
]
PREVIEW_MARKERS = (
    "!nd_",
    "WB_PRV",
    "WB_DFT",
    "imageView2",
    "watermark",
    "sns-webpic",
)


def classify_input(value: str) -> str:
    lower = value.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        if any(marker.lower() in lower for marker in PREVIEW_MARKERS):
            return "preview_url"
        if "xhscdn.com" in lower or "xhsci.com" in lower:
            return "raw_url"
        return "url"
    return "raw_key"


def classify_final_url(value: str) -> str:
    lower = value.lower()
    if any(marker.lower() in lower for marker in PREVIEW_MARKERS):
        return "preview_url"
    if "xhscdn.com" in lower or "xhsci.com" in lower:
        return "original_url"
    return "unknown_url"


def extract_raw_key(value: str) -> str:
    kind = classify_input(value)
    if kind == "raw_key":
        return value.strip()

    parsed = urlparse(value)
    path = unquote(parsed.path or "").strip("/")
    if not path:
        raise ValueError(f"cannot extract raw key from: {value}")

    key = path.split("/")[-1]
    if "!" in key:
        key = key.split("!", 1)[0]
    if "?" in key:
        key = key.split("?", 1)[0]
    return key


def build_candidate_urls(raw_key: str, hosts: Iterable[str]) -> List[str]:
    key = raw_key.lstrip("/")
    urls = []
    for host in hosts:
        base = host.rstrip("/")
        urls.append(f"{base}/notes_pre_post/{key}")
        urls.append(f"{base}/{key}")
        # Live Photo（实况）走 note_pre_post_uhdr 路径（注意单数 note）
        urls.append(f"{base}/note_pre_post_uhdr/{key}")
    return urls


def sniff_size(content: bytes, content_type: str) -> Optional[Tuple[int, int]]:
    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24:
        width, height = struct.unpack(">II", content[16:24])
        return width, height

    if content.startswith(b"\xff\xd8"):
        stream = BytesIO(content)
        stream.read(2)
        while True:
            marker_prefix = stream.read(1)
            if not marker_prefix:
                break
            if marker_prefix != b"\xff":
                continue
            marker = stream.read(1)
            while marker == b"\xff":
                marker = stream.read(1)
            if marker in {
                b"\xc0",
                b"\xc1",
                b"\xc2",
                b"\xc3",
                b"\xc5",
                b"\xc6",
                b"\xc7",
                b"\xc9",
                b"\xca",
                b"\xcb",
                b"\xcd",
                b"\xce",
                b"\xcf",
            }:
                block = stream.read(7)
                if len(block) >= 5:
                    height, width = struct.unpack(">HH", block[1:5])
                    return width, height
                break
            size_bytes = stream.read(2)
            if len(size_bytes) != 2:
                break
            block_size = struct.unpack(">H", size_bytes)[0]
            stream.seek(max(block_size - 2, 0), os.SEEK_CUR)

    if content.startswith(b"RIFF") and content[8:12] == b"WEBP" and len(content) >= 30:
        chunk_type = content[12:16]
        if chunk_type == b"VP8X" and len(content) >= 30:
            width = 1 + int.from_bytes(content[24:27], "little")
            height = 1 + int.from_bytes(content[27:30], "little")
            return width, height

    if content_type.startswith("image/"):
        return None
    return None


def probe(session: requests.Session, url: str, timeout: int = 20) -> Tuple[bool, dict]:
    try:
        r = session.get(url, timeout=timeout, stream=True)
        r.raise_for_status()
        content_type = (r.headers.get("content-type") or "").lower()
        body = r.raw.read(512 * 1024, decode_content=True)
        info = {
            "url": url,
            "status": r.status_code,
            "content_type": content_type,
            "bytes_sampled": len(body),
            "size": sniff_size(body, content_type),
            "url_kind": classify_final_url(url),
        }
        return True, info
    except Exception as e:
        return False, {"url": url, "error": str(e), "url_kind": classify_final_url(url)}


def sniff_ext(content: bytes) -> str:
    """按文件魔数判定真实后缀，不信 HTTP content-type（小红书 CDN 会乱标）。"""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8"):
        return ".jpg"
    # HEIF: ftyp box 在偏移 4 处，brand 含 heic/heix/hevc/hevx/mif1/msf1
    if len(content) >= 12 and content[4:8] == b"ftyp" and content[8:12] in (
        b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1",
    ):
        return ".heic"
    if content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def download(session: requests.Session, url: str, out_dir: str, index: int) -> dict:
    r = session.get(url, timeout=30)
    r.raise_for_status()

    content_type = (r.headers.get("content-type") or "").lower()
    # 注意：content-type 不可靠（如标 image/jpeg 实际是 HEIF/PNG），以后缀一律按魔数判定
    ext = sniff_ext(r.content)
    path = os.path.join(out_dir, f"original_{index}{ext}")

    with open(path, "wb") as f:
        f.write(r.content)

    return {
        "path": path,
        "content_type": content_type,
        "bytes": len(r.content),
        "size": sniff_size(r.content, content_type),
        "url": url,
        "url_kind": classify_final_url(url),
    }


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.xiaohongshu.com/",
    })
    return session


def main():
    parser = argparse.ArgumentParser(
        description="Normalize Xiaohongshu preview URLs to raw keys, probe candidate original URLs, and report whether each result is original or preview-only."
    )
    parser.add_argument("--out-dir")
    parser.add_argument("--url", action="append", default=[], help="Image URL. Can be preview URL or raw CDN URL.")
    parser.add_argument("--key", action="append", default=[], help="Raw CDN key without host.")
    parser.add_argument("--host", action="append", default=[], help="Candidate raw host. Can be passed multiple times.")
    parser.add_argument("--probe-only", action="store_true", help="Only classify / normalize / probe candidate URLs, do not download.")
    args = parser.parse_args()

    if not args.url and not args.key:
        parser.error("at least one of --url or --key is required")

    hosts = args.host or DEFAULT_HOSTS
    session = make_session()

    raw_keys: List[str] = []
    seen = set()
    for value in [*args.url, *args.key]:
        raw_key = extract_raw_key(value)
        if raw_key not in seen:
            seen.add(raw_key)
            raw_keys.append(raw_key)
        print(json.dumps({
            "input": value,
            "kind": classify_input(value),
            "raw_key": raw_key,
            "candidate_urls": build_candidate_urls(raw_key, hosts),
        }, ensure_ascii=False))

    if args.probe_only:
        for raw_key in raw_keys:
            for url in build_candidate_urls(raw_key, hosts):
                ok, info = probe(session, url)
                print(json.dumps({"probe_ok": ok, **info}, ensure_ascii=False))
        return

    if not args.out_dir:
        parser.error("--out-dir is required unless --probe-only is set")

    os.makedirs(args.out_dir, exist_ok=True)

    download_index = 1
    for raw_key in raw_keys:
        selected_url = None
        selected_probe = None
        for url in build_candidate_urls(raw_key, hosts):
            ok, info = probe(session, url)
            print(json.dumps({"probe_ok": ok, **info}, ensure_ascii=False))
            if ok and (info.get("content_type") or "").startswith("image/"):
                selected_url = url
                selected_probe = info
                break

        if not selected_url:
            print(json.dumps({
                "raw_key": raw_key,
                "status": "failed",
                "downloaded": False,
                "reason": "no_working_candidate_url",
            }, ensure_ascii=False))
            continue

        info = download(session, selected_url, args.out_dir, download_index)
        status = "original_success" if info["url_kind"] == "original_url" else "preview_only"
        print(json.dumps({
            "status": status,
            "downloaded": True,
            "probe": selected_probe,
            **info,
        }, ensure_ascii=False))
        download_index += 1


if __name__ == "__main__":
    main()
