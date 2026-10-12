"""Download every external resource the project needs, pinned and verified.

Run from the repository root:

    python scripts/fetch_resources.py                 # everything (about 30 MB)
    python scripts/fetch_resources.py --only axe benchmark
    python scripts/fetch_resources.py --full-benchmark   # also the 20 MB full dataset

What gets downloaded (into data/, which git ignores):

    axe        axe-core accessibility engine, one JavaScript file, SHA-256 checked
    benchmark  AccessGuru benchmark CSVs + the ACCESS (Huang et al. 2024) set
    wcag       W3C WCAG 2.2 "Understanding" and "Techniques" pages (RAG corpus)
    axe-rules  axe-core rule metadata: rule -> WCAG criterion, impact, help text

Every source is pinned to an exact version or commit so results are
reproducible, and every single file has a known SHA-256 so a corrupted or
tampered download fails loudly instead of silently changing the benchmark.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.getenv("DATA_DIR", ROOT / "data"))
USER_AGENT = "accessibility-repair-agent-setup/0.1 (+https://github.com/Harshit-rai-1506/accessibility-repair-agent)"

# --- Pinned sources -------------------------------------------------------------

AXE_VERSION = os.getenv("AXE_VERSION", "4.12.1").strip() or "4.12.1"
AXE_SHA256 = {
    # Official hash published by jsDelivr for axe-core@4.12.1/axe.min.js
    "4.12.1": "66a8aaa95a8b044a7fd74a5435873bf04ff65a1ca75567c921b7509742085a14",
}

ACCESSGURU_REPO = "NadeenAhmad/AccessGuruLLM"
ACCESSGURU_COMMIT = "bf45666aa281af7cca23aaabd9bfe3bd802b9036"
BENCHMARK_FILES = {
    # path in their repo: expected SHA-256
    "data/accessguru_dataset/accessguru_sampled_syntax_layout_dataset.csv": "aaafa12af7d7084cff4c34239131a4c27cc4a0c339b202b19ba0b0ace81fafa4",
    "data/accessguru_dataset/accessguru_sampled_semantic_violations.csv": "498105a2d12fc7dcb8425bbf45e4f7978450d090616802ceebbe063e9ba00fd3",
    "data/baseline_two_dataset.csv": "bc446df99725c884f0c490c32b228c023b9dbf621535c3078afe77971464c55e",
    "data/prompts_support/WCAGGuidelines.csv": "045b37132c687760ed17804e1fee03a0d9a71055b13b9527bbbad838bc44dcd4",
    "data/prompts_support/mapping_dict_file.json": "f8988dec9014fd4c25fa10d9cfd12c809bf33d88f3b6832aef7c6c0a6b2a7778",
    "data/prompts_support/violation_taxonomy.csv": "f076f836146696208cf242b9ca87e291f75456e916df14e5d81eaca6911b8792",
    "data/prompts_support/violations_short_description.json": "921e1be4ce0bc8d59db0369c115d0ba2e0feeb42ec6f7ed26e47c92f5a5ced45",
}
FULL_BENCHMARK_FILES = {
    "data/accessguru_dataset/Original_full_data_new.csv": "d4840d57cfb46f7217060f81c175b296c9c2e854b28fae8092c4b90f38fa0479",
}

WCAG_REPO = "w3c/wcag"
WCAG_COMMIT = "23bad5904949904275a08f1c6e395b78a02aa6f3"
WCAG_PATHS = [
    "understanding/",
    "guidelines/sc/",
    "techniques/aria/",
    "techniques/client-side-script/",
    "techniques/css/",
    "techniques/failures/",
    "techniques/general/",
    "techniques/html/",
    "techniques/text/",
]

AXE_REPO = "dequelabs/axe-core"
AXE_RULES_PATHS = ["lib/rules/", "locales/_template.json"]

LICENSES = {
    "axe": "MPL-2.0 (Deque Systems)",
    "benchmark": "AccessGuru dataset CC BY 4.0, code MIT (Fathallah, Hernández, Staab, ASSETS 2025)",
    "wcag": "W3C Document License",
    "axe-rules": "MPL-2.0 (Deque Systems)",
}


# --- Small helpers -----------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def http_get(url: str, *, attempts: int = 3, timeout: int = 60) -> bytes:
    """GET a URL with a few retries. Uses HTTPS_PROXY from the environment if set."""
    last_error: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:  # network errors vary a lot by platform
            last_error = e
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"download failed: {url} ({last_error})")


def write_verified(data: bytes, dest: Path, expected_sha256: str | None) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if expected_sha256 and actual != expected_sha256:
        raise RuntimeError(
            f"checksum mismatch for {dest.name}\n  expected {expected_sha256}\n  got      {actual}"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)


def is_current(dest: Path, expected_sha256: str | None) -> bool:
    if not dest.exists():
        return False
    return expected_sha256 is None or sha256_of(dest) == expected_sha256


def _force_remove(func, path, _exc):
    # Git marks pack files read-only, which makes rmtree fail on Windows.
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree(path: Path) -> None:
    if path.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_force_remove)
        else:
            shutil.rmtree(path, onerror=_force_remove)


def have_git() -> bool:
    return shutil.which("git") is not None


def git(*args: str, cwd: Path | None = None) -> None:
    # autocrlf off: keep files byte-identical to GitHub so checksums match on Windows too.
    cmd = ["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "advice.detachedHead=false", *args]
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def github_subset(repo: str, ref: str, paths: list[str], dest: Path, method: str) -> None:
    """Copy selected folders/files of a GitHub repo at an exact ref into dest.

    Tries a sparse git checkout first (downloads only what is needed), then
    falls back to the repository zip archive if git is missing or blocked.
    """
    errors = []
    if method in ("auto", "git") and have_git():
        try:
            _github_subset_git(repo, ref, paths, dest)
            return
        except Exception as e:
            errors.append(f"git: {e}")
    if method in ("auto", "http"):
        try:
            _github_subset_zip(repo, ref, paths, dest)
            return
        except Exception as e:
            errors.append(f"zip: {e}")
    raise RuntimeError(f"could not fetch {repo}@{ref}: " + ("; ".join(errors) or "no method available"))


def _github_subset_git(repo: str, ref: str, paths: list[str], dest: Path) -> None:
    with tempfile.TemporaryDirectory(dir=DATA, ignore_cleanup_errors=True) as tmp_str:
        tmp = Path(tmp_str) / "repo"
        git("init", "-q", str(tmp))
        git("remote", "add", "origin", f"https://github.com/{repo}.git", cwd=tmp)
        patterns = ["/" + p for p in paths]
        git("sparse-checkout", "set", "--no-cone", *patterns, cwd=tmp)
        git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", ref, cwd=tmp)
        git("checkout", "-q", "FETCH_HEAD", cwd=tmp)
        rmtree(tmp / ".git")
        _move_into(tmp, dest)
        rmtree(tmp)


def _github_subset_zip(repo: str, ref: str, paths: list[str], dest: Path) -> None:
    owner, name = repo.split("/")
    data = http_get(f"https://codeload.github.com/{owner}/{name}/zip/{ref}", timeout=300)
    with tempfile.TemporaryDirectory(dir=DATA, ignore_cleanup_errors=True) as tmp_str:
        tmp = Path(tmp_str) / "repo"
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.infolist():
                # archive paths look like "<repo>-<ref>/understanding/foo.html"
                rel = member.filename.split("/", 1)[1] if "/" in member.filename else ""
                if not rel or member.is_dir():
                    continue
                if any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in paths):
                    out = tmp / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(zf.read(member))
        _move_into(tmp, dest)
        rmtree(tmp)


def _move_into(src: Path, dest: Path) -> None:
    if not any(src.iterdir()):
        raise RuntimeError("nothing matched the requested paths")
    rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))


# --- Resources -------------------------------------------------------------------

def fetch_axe(force: bool, method: str) -> dict:
    dest = DATA / "vendor" / f"axe-core-{AXE_VERSION}.min.js"
    expected = AXE_SHA256.get(AXE_VERSION)
    if expected is None:
        log(f"  ! no pinned checksum for axe-core {AXE_VERSION}; it will be downloaded unverified")
    if not force and is_current(dest, expected):
        log(f"  = already present: {dest.relative_to(ROOT)}")
    else:
        urls = [
            f"https://cdn.jsdelivr.net/npm/axe-core@{AXE_VERSION}/axe.min.js",
            f"https://unpkg.com/axe-core@{AXE_VERSION}/axe.min.js",
        ]
        data = None
        for url in urls:
            try:
                data = http_get(url)
                break
            except RuntimeError as e:
                log(f"  ! {e}")
        if data is None:
            # Last resort: the npm registry tarball, which contains the same file.
            tgz = http_get(f"https://registry.npmjs.org/axe-core/-/axe-core-{AXE_VERSION}.tgz")
            with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tf:
                member = tf.extractfile("package/axe.min.js")
                if member is None:
                    raise RuntimeError("axe.min.js not found in npm tarball")
                data = member.read()
        write_verified(data, dest, expected)
        log(f"  + downloaded {dest.relative_to(ROOT)}")
    return {
        "source": f"npm:axe-core@{AXE_VERSION}",
        "files": {str(dest.relative_to(DATA)): sha256_of(dest)},
    }


def fetch_benchmark(force: bool, method: str, full: bool) -> dict:
    files = dict(BENCHMARK_FILES)
    if full:
        files.update(FULL_BENCHMARK_FILES)
    base = DATA / "benchmark" / "accessguru"
    missing = {p: h for p, h in files.items() if force or not is_current(base / p, h)}
    if not missing:
        log("  = already present: data/benchmark/accessguru")
    else:
        failed = {}
        if method in ("auto", "http"):
            for path, expected in missing.items():
                url = f"https://raw.githubusercontent.com/{ACCESSGURU_REPO}/{ACCESSGURU_COMMIT}/{path}"
                try:
                    write_verified(http_get(url, timeout=120), base / path, expected)
                    log(f"  + {path}")
                except RuntimeError as e:
                    failed[path] = expected
                    if method == "http":
                        raise
                    log(f"  ! {path}: {e}")
        else:
            failed = missing
        if failed:
            log("  ~ retrying missing files with a sparse git checkout")
            staging = DATA / "benchmark" / "_staging"
            github_subset(ACCESSGURU_REPO, ACCESSGURU_COMMIT, list(failed), staging, "git")
            for path, expected in failed.items():
                write_verified((staging / path).read_bytes(), base / path, expected)
                log(f"  + {path}")
            rmtree(staging)
    return {
        "source": f"github:{ACCESSGURU_REPO}@{ACCESSGURU_COMMIT}",
        "files": {p: h for p, h in files.items()},
    }


def fetch_tree(repo: str, ref: str, paths: list[str], dest: Path, force: bool, method: str) -> dict:
    marker = dest / ".fetched.json"
    if not force and marker.exists() and json.loads(marker.read_text()).get("ref") == ref:
        log(f"  = already present: {dest.relative_to(ROOT)}")
    else:
        github_subset(repo, ref, paths, dest, method)
        marker.write_text(json.dumps({"repo": repo, "ref": ref, "paths": paths}, indent=2))
        log(f"  + {dest.relative_to(ROOT)}")
    count = sum(1 for p in dest.rglob("*") if p.is_file() and p.name != ".fetched.json")
    return {"source": f"github:{repo}@{ref}", "paths": paths, "file_count": count}


# --- Entry point -------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="+", choices=["axe", "benchmark", "wcag", "axe-rules"],
                        help="fetch only these resources (default: all)")
    parser.add_argument("--full-benchmark", action="store_true", help="also fetch the 20 MB full dataset (3,524 rows)")
    parser.add_argument("--force", action="store_true", help="re-download even if files are present")
    parser.add_argument("--method", choices=["auto", "git", "http"], default="auto",
                        help="how to download GitHub content (default: try both)")
    args = parser.parse_args()

    wanted = args.only or ["axe", "benchmark", "wcag", "axe-rules"]
    DATA.mkdir(parents=True, exist_ok=True)
    manifest_path = DATA / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    steps = {
        "axe": lambda: fetch_axe(args.force, args.method),
        "benchmark": lambda: fetch_benchmark(args.force, args.method, args.full_benchmark),
        "wcag": lambda: fetch_tree(WCAG_REPO, WCAG_COMMIT, WCAG_PATHS,
                                   DATA / "corpus" / "wcag", args.force, args.method),
        "axe-rules": lambda: fetch_tree(AXE_REPO, f"v{AXE_VERSION}", AXE_RULES_PATHS,
                                        DATA / "corpus" / "axe-rules", args.force, args.method),
    }

    failures = 0
    for name in wanted:
        log(f"[{name}]")
        try:
            entry = steps[name]()
            entry["license"] = LICENSES[name]
            entry["fetched_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            manifest[name] = entry
        except Exception as e:
            failures += 1
            log(f"  x failed: {e}")

    manifest_path.write_text(json.dumps(manifest, indent=2))
    log(f"\nManifest written to {manifest_path.relative_to(ROOT)}")
    if failures:
        log(f"{failures} resource(s) failed. Check your internet connection and run the script again.")
        return 1
    log("All resources ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
