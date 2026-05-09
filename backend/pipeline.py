"""
Simple Nia ingestion + chat pipeline.

Drop PDFs / CSVs / text files into ./data, then:

    python pipeline.py ingest                  # ingest everything in ./data
    python pipeline.py ingest path/to/file     # ingest one file
    python pipeline.py list                    # show what's been ingested
    python pipeline.py chat "your question"    # ask across all ingested sources
    python pipeline.py ask <doc-name> "..."    # ask a single uploaded doc with citations

Sources are tracked in pipeline.json. PDFs/CSVs/Excel are uploaded individually;
plain text files (.txt, .md, ...) are indexed by registering ./data as a local
folder source.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "../data"
MANIFEST = ROOT / "pipeline.json"

UPLOADABLE_EXTS = {".pdf", ".xls", ".xlsx"}
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log",
    ".json", ".yaml", ".yml",
    ".csv", ".tsv",
}

UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)

NIA = shutil.which("nia.cmd") or shutil.which("nia") or "nia"


def nia(*args: str, stream: bool = False) -> tuple[int, str, str]:
    if stream:
        proc = subprocess.run([NIA, *args])
        return proc.returncode, "", ""
    proc = subprocess.run(
        [NIA, *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {"docs": [], "local_folder": None}


def save_manifest(m: dict) -> None:
    MANIFEST.write_text(json.dumps(m, indent=2), encoding="utf-8")


def find_uuid(*texts: str) -> str | None:
    for t in texts:
        m = UUID_RE.search(t)
        if m:
            return m.group(0)
    return None


def upload_doc(file_path: Path, name: str) -> str | None:
    print(f"  uploading {file_path.name} as '{name}'...")
    code, out, err = nia("sources", "upload", str(file_path), "--name", name, "--verbose")
    if code != 0:
        first = (err or out).strip().splitlines()
        print(f"  upload failed: {first[0] if first else 'unknown error'}")
        return None
    sid = find_uuid(out, err)
    print(f"  uploaded -> id={sid or '(not parsed)'}")
    return sid


def register_local_folder(folder: Path) -> str | None:
    print(f"  registering local folder: {folder}")
    code, out, err = nia("local", "add", str(folder), "--verbose")
    if code != 0:
        first = (err or out).strip().splitlines()
        print(f"  local add failed: {first[0] if first else 'unknown'}")
        return None
    sid = find_uuid(out, err)
    print(f"  registered -> id={sid or '(not parsed; check `nia local status`)'}")
    return sid


def collect_files(target: Path) -> tuple[list[Path], list[Path], list[Path], Path]:
    if target.is_file():
        files = [target]
        folder = target.parent
    else:
        files = sorted(p for p in target.rglob("*") if p.is_file())
        folder = target
    text = [f for f in files if f.suffix.lower() in TEXT_EXTS]
    upload = [f for f in files if f.suffix.lower() in UPLOADABLE_EXTS]
    skipped = [f for f in files if f.suffix.lower() not in TEXT_EXTS | UPLOADABLE_EXTS]
    return text, upload, skipped, folder


def cmd_ingest(args: list[str]) -> None:
    target = Path(args[0]).resolve() if args else DATA_DIR
    if not target.exists():
        target.mkdir(parents=True)
        print(f"created empty {target} -- drop files in there and re-run.")
        return

    m = load_manifest()
    seen = {d["file"] for d in m["docs"]}
    text_files, upload_files, skipped, folder = collect_files(target)

    if text_files:
        if m.get("local_folder") is None:
            sid = register_local_folder(folder)
            m["local_folder"] = {
                "id": sid,
                "path": str(folder),
                "name": folder.name,
                "file_count": len(text_files),
            }
            save_manifest(m)
        else:
            print(
                f"  local folder already registered "
                f"(id={m['local_folder'].get('id') or '?'}); "
                "edits sync via `nia local sync`."
            )

    for f in upload_files:
        rel = str(f.relative_to(folder))
        if rel in seen:
            print(f"  skip (already ingested): {rel}")
            continue
        sid = upload_doc(f, name=f.stem)
        if sid is None:
            print(f"  -> not recording {rel} in manifest (upload failed)")
            continue
        m["docs"].append(
            {
                "file": rel,
                "name": f.stem,
                "id": sid,
                "type": f.suffix.lower().lstrip("."),
            }
        )
        save_manifest(m)

    if skipped:
        names = ", ".join(f.name for f in skipped[:10])
        more = f" (+{len(skipped) - 10} more)" if len(skipped) > 10 else ""
        print(f"\n  skipped (unsupported): {names}{more}")
    print("\ndone. indexing runs server-side -- usually 1-5 min before chat works.")


def cmd_list(_args: list[str]) -> None:
    m = load_manifest()
    lf = m.get("local_folder")
    if lf:
        print(f"local folder: {lf['name']}  id={lf.get('id') or '?'}")
        print(f"  path: {lf['path']}")
    else:
        print("local folder: (none registered)")
    print()
    if not m["docs"]:
        print("uploaded docs: (none)")
        return
    print("uploaded docs:")
    for d in m["docs"]:
        print(
            f"  - {d['name']:30s}  type={d['type']:6s}  id={d.get('id') or '?'}"
        )


def cmd_chat(args: list[str]) -> None:
    if not args:
        print('usage: python pipeline.py chat "your question"')
        sys.exit(2)
    question = " ".join(args)
    m = load_manifest()
    doc_ids = [d["id"] for d in m["docs"] if d.get("id")]
    lf_id = (m.get("local_folder") or {}).get("id")
    if not doc_ids and not lf_id:
        print("nothing ingested. run: python pipeline.py ingest")
        sys.exit(2)
    cli_args = ["search", "query", question]
    if doc_ids:
        cli_args += ["--docs", ",".join(doc_ids)]
    if lf_id:
        cli_args += ["--local-folders", lf_id]
    print(f"$ nia {' '.join(cli_args)}\n")
    code, _, _ = nia(*cli_args, stream=True)
    sys.exit(code)


def cmd_ask(args: list[str]) -> None:
    if len(args) < 2:
        print('usage: python pipeline.py ask <doc-name-or-id> "your question"')
        sys.exit(2)
    target, *qparts = args
    question = " ".join(qparts)
    m = load_manifest()
    match = next(
        (
            d
            for d in m["docs"]
            if d["name"] == target or d.get("id") == target or d["file"] == target
        ),
        None,
    )
    if not match or not match.get("id"):
        print(f"no doc matched '{target}'. run: python pipeline.py list")
        sys.exit(2)
    print(f"$ nia document agent {match['id']} {question!r} --stream\n")
    code, _, _ = nia("document", "agent", match["id"], question, "--stream", stream=True)
    sys.exit(code)


COMMANDS = {
    "ingest": cmd_ingest,
    "list": cmd_list,
    "chat": cmd_chat,
    "ask": cmd_ask,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(0 if len(sys.argv) == 1 else 2)
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
