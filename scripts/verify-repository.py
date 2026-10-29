"""公开源码统一验证：四文件、实际待提交内容、定向业务测试与网站构建。"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_repository import MANIFEST, scan_payload

DOCUMENTS = ("PROJECT_PLAN.md", "AGENTS.md", "README.md", "TASKS.md")
BUSINESS_TESTS = (
    "test_web_api", "test_shared_credits", "test_research_library", "test_pymatgen_credits",
    "test_pymatgen_api", "test_abtem_api", "test_literature_api", "test_xrd_peak_standard",
    "test_jade_zid", "test_jade_structure_reader", "test_admin_center", "test_science_queue",
    "test_public_entrypoint",
)


def four_files(root):
    failures = []
    for name in DOCUMENTS:
        path = root / name
        try:
            if not path.is_file():
                failures.append(name + "：不存在或不是文件")
            elif not path.read_text(encoding="utf-8-sig").strip():
                failures.append(name + "：内容为空")
        except (OSError, UnicodeError):
            failures.append(name + "：无法读取")
    if failures:
        raise ValueError("；".join(failures))
    print("PASS 四文件均存在、为文件且内容非空", flush=True)


def index_payload(root):
    entries = subprocess.run(["git", "ls-files", "--stage", "-z"], cwd=root,
                             capture_output=True, check=True).stdout
    objects = []
    for item in entries.rstrip(b"\0").split(b"\0") if entries else []:
        header, name = item.split(b"\t", 1)
        mode, object_id, stage = header.split()
        if mode not in (b"100644", b"100755") or stage != b"0":
            raise ValueError("公开检查失败：暂存区存在链接、子模块或未解决合并。")
        objects.append((name.decode("utf-8"), object_id))
    data = subprocess.run(["git", "cat-file", "--batch"], cwd=root,
                          input=b"".join(value + b"\n" for _, value in objects),
                          capture_output=True, check=True).stdout if objects else b""
    payload, offset = {}, 0
    for name, object_id in objects:
        end = data.index(b"\n", offset)
        header = data[offset:end].split()
        if len(header) != 3 or header[:2] != [object_id, b"blob"]:
            raise ValueError("公开检查失败：无法核验暂存对象。")
        size = int(header[2])
        if size > 2 * 1024 * 1024:
            raise ValueError("公开检查失败：暂存文件超过2MiB。")
        start = end + 1
        payload[name] = data[start:start + size]
        if len(payload[name]) != size or data[start + size:start + size + 1] != b"\n":
            raise ValueError("公开检查失败：暂存对象不完整。")
        offset = start + size + 1
    return payload


def public_files(root, staged=False):
    indexed = None
    if (root / ".git").exists():
        indexed = index_payload(root)
        names = list(indexed)
        for name in DOCUMENTS:
            try:
                nonempty = indexed.get(name, b"").decode("utf-8-sig").strip()
            except UnicodeError:
                nonempty = ""
            if not nonempty:
                raise ValueError("公开检查失败：暂存区四文件缺失、不可读或为空：" + name)
    else:
        manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
        names = list(manifest["files"]) + [MANIFEST]
    if not names:
        raise ValueError("没有可核验的公开文件，请先生成副本或暂存源码。")
    payload = {}
    for name in names:
        path = root / name
        if (path.is_symlink() or not path.resolve().is_relative_to(root)
                or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024):
            raise ValueError("公开文件越界、缺失、不是普通文件或超过2MiB：" + name)
        payload[name] = indexed[name] if staged and indexed is not None else path.read_bytes()
    failures = scan_payload(payload)
    if indexed is not None and not staged:
        failures.extend(scan_payload(indexed))
    if failures:
        raise ValueError("公开内容检查失败：" + json.dumps(failures, ensure_ascii=False))
    label = "（含暂存区）" if indexed is not None else ""
    print(f"PASS {len(payload)} 个公开文件的路径及内容检查{label}", flush=True)
    return len(payload)


def run(command, root, env):
    result = subprocess.run(command, cwd=root, env=env)
    if result.returncode:
        raise ValueError("验证命令失败：" + " ".join(str(item) for item in command[:4]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--checks-only", action="store_true")
    parser.add_argument("--skip-web", action="store_true")
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        four_files(root)
        count = public_files(root, args.staged)
        cache = root / ".cache"
        (cache / "tmp").mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
                   TMP=str(cache / "tmp"), TEMP=str(cache / "tmp"), TMPDIR=str(cache / "tmp"),
                   PIP_CACHE_DIR=str(cache / "pip"), npm_config_cache=str(cache / "npm"),
                   PYTHONPATH=str(root / "tests"), npm_config_update_notifier="false")
        if not args.checks_only:
            missing = [name for name in BUSINESS_TESTS if not (root / "tests" / (name + ".py")).is_file()]
            if missing:
                raise ValueError("缺少定向业务回归：" + ", ".join(missing))
            run([sys.executable, "-B", "-X", "utf8", "-m", "unittest", "-v", *BUSINESS_TESTS], root, env)
            if not args.skip_web:
                node = shutil.which("node")
                npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
                if not node or not npm or not (root / "web/node_modules").is_dir():
                    raise ValueError("缺少Node.js或前端依赖，请先在 ./web/ 执行 npm ci。")
                test_files = sorted((root / "web/src").glob("*.test.mjs"))
                if not test_files:
                    raise ValueError("缺少前端回归测试。")
                run([node, "--test", *(str(path) for path in test_files)], root / "web", env)
                run([npm, "run", "build"], root / "web", env)
        receipt = {"checked_at": datetime.now(timezone.utc).isoformat(), "files": count,
                   "four_files": "passed", "public_content": "passed",
                   "business_tests": "not_run" if args.checks_only else "passed",
                   "web": "not_run" if args.checks_only or args.skip_web else "passed",
                   "staged": args.staged or (root / ".git").exists(), "production_deployment": "not_changed"}
        (cache / "repository-verification.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("PASS 公开源码本次验证通过；生产部署与真实业务验收需单独记录。")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print("FAIL " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
