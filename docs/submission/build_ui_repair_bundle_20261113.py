"""Build a non-overwriting 2026-11-13 submission copy with the demo UI repair."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE_SUBMISSION = ROOT / "dist/final/20261113_正式提交"
OUTPUT_SUBMISSION = ROOT / "dist/final/20261113_正式提交_修复版"
TEAM_FOLDER = "AIC-2026-56970712-场景设计-6G链路自适应家庭AI推理系统"
ARCHIVE_NAME = f"{TEAM_FOLDER}-代码与数据.zip"
TEAM_PATH = Path("04_百度网盘文件夹") / TEAM_FOLDER
SOURCE_ARCHIVE = SOURCE_SUBMISSION / TEAM_PATH / ARCHIVE_NAME
OUTPUT_ARCHIVE = OUTPUT_SUBMISSION / TEAM_PATH / ARCHIVE_NAME
BASE_COMMIT = "dfd5ff026e044cf5059eb5a6fd945e2e2b552c48"

OVERLAY_FILES = [
    "openvela_smart_home/dashboard/bridge_server.py",
    "openvela_smart_home/dashboard/web_panel.html",
    "openvela_smart_home/dashboard/tests/test_local_explicit_contract.py",
    "openvela_smart_home/dashboard/tests/test_demo_panel_contract.py",
    "docs/intrinsic_v6/demo_repair_20261113.md",
    "docs/intrinsic_v6/checks_20261113/README.md",
    "docs/intrinsic_v6/checks_20261113/result.json",
    "docs/intrinsic_v6/checks_20261113/python_tests.txt",
    "docs/intrinsic_v6/checks_20261113/historical_compatibility_tests.txt",
    "docs/intrinsic_v6/checks_20261113/dashboard_client_tests.txt",
    "docs/submission/build_ui_repair_bundle_20261113.py",
]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_archive(old_zip: Path, new_zip: Path) -> dict:
    overlays = {rel: (ROOT / rel).read_bytes() for rel in OVERLAY_FILES}
    old_zip_hash = sha(old_zip.read_bytes())
    temp_zip = new_zip.with_suffix(".zip.tmp")
    previous = {}
    written = set()

    with zipfile.ZipFile(old_zip, "r") as source:
        roots = [n for n in source.namelist() if n.endswith("/") and n.count("/") == 1]
        if len(roots) != 1:
            raise RuntimeError(f"Expected one source root folder; found {roots}")
        old_prefix = roots[0]
        new_prefix = old_prefix[:-1] + "_20261113_ui_fix/"
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as target:
            for info in source.infolist():
                name = info.filename
                relative = name[len(old_prefix):] if name.startswith(old_prefix) else None
                data = source.read(info) if not info.is_dir() else b""
                output_name = new_prefix + relative if relative is not None else name
                if relative in overlays:
                    previous[relative] = sha(data)
                    data = overlays[relative]
                    written.add(relative)
                new_info = copy.copy(info)
                new_info.filename = output_name
                target.writestr(new_info, data)

            for relative, data in overlays.items():
                if relative in written:
                    continue
                info = zipfile.ZipInfo(new_prefix + relative, (2026, 11, 13, 8, 30, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                target.writestr(info, data)
                written.add(relative)

            update_note = (
                "# 2026-11-13演示界面修复版\n\n"
                "本包基于原代码包增量更新：修正规则演示快捷项、房间温湿度查询澄清、初始空状态及请求回查提示。"
                "补入修复回归报告、原始测试输出与新增测试。\n\n"
                "完整补充回归427项和258个子测试通过；历史兼容5项、前端请求恢复5项通过。"
                "结果只代表软件测试，不代表真实家居、用户试用或6G空口验证。\n\n"
                "运行前先停止旧服务，再于项目根目录执行：\n\n"
                "```powershell\npython -X utf8 .\\docs\\demo_v4\\live_v4.py --rules-only\n```\n\n"
                "浏览器打开 http://127.0.0.1:8879/ 并按Ctrl+F5。明确亮度请带百分比；"
                "规则模式不支持‘有点冷’和‘不透气’的模型理解。答辩PPT和方案仍为2026-11-12封版。\n"
            ).encode("utf-8")
            note_path = "更新说明_20261113.md"
            target.writestr(new_prefix + note_path, update_note)
            written.add(note_path)

            file_manifest = {}
            for relative in sorted(written):
                data = overlays.get(relative)
                if data is None:
                    data = update_note
                file_manifest[relative] = {
                    "bytes": len(data),
                    "sha256": sha(data),
                    "previous_sha256": previous.get(relative),
                }
            package_manifest = {
                "schema": "sensehome.ui-repair-overlay.v1",
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "base_commit": BASE_COMMIT,
                "previous_archive_sha256": old_zip_hash,
                "new_internal_root": new_prefix,
                "entries": file_manifest,
            }
            target.writestr(
                new_prefix + "UPDATE_SHA256_20261113.json",
                json.dumps(package_manifest, ensure_ascii=False, indent=2) + "\n",
            )

    with zipfile.ZipFile(temp_zip, "r") as check:
        if check.testzip() is not None:
            raise RuntimeError("New source archive failed its CRC check")
        for relative in OVERLAY_FILES:
            if sha(check.read(new_prefix + relative)) != sha(overlays[relative]):
                raise RuntimeError(f"Overlay hash check failed: {relative}")
    temp_zip.replace(new_zip)
    return {
        "previous_archive_sha256": old_zip_hash,
        "new_archive_sha256": sha(new_zip.read_bytes()),
        "new_archive_bytes": new_zip.stat().st_size,
        "new_internal_root": new_prefix,
        "overlay_files": sorted(written),
        "zip_crc_and_overlay_hashes_passed": True,
    }


def main() -> None:
    if OUTPUT_SUBMISSION.exists():
        raise FileExistsError(f"Refusing to overwrite: {OUTPUT_SUBMISSION}")
    if not SOURCE_ARCHIVE.is_file():
        raise FileNotFoundError(SOURCE_ARCHIVE)
    for relative in OVERLAY_FILES:
        if not (ROOT / relative).is_file():
            raise FileNotFoundError(ROOT / relative)

    shutil.copytree(SOURCE_SUBMISSION, OUTPUT_SUBMISSION)
    archive = build_archive(SOURCE_ARCHIVE, OUTPUT_ARCHIVE)
    team_dir = OUTPUT_SUBMISSION / TEAM_PATH
    intro_paths = [
        OUTPUT_SUBMISSION / "作品简介_300字符.txt",
        team_dir / f"{TEAM_FOLDER}-作品简介.txt",
    ]
    for path in intro_paths:
        text = path.read_text(encoding="utf-8-sig")
        text = text.replace("423项测试及258个子测试", "427项测试及258个子测试")
        path.write_text(text, encoding="utf-8-sig")

    form_path = OUTPUT_SUBMISSION / "00_报名字段_复制粘贴.txt"
    form = form_path.read_text(encoding="utf-8-sig")
    form = re.sub(r"(?m)^代码ZIP SHA-256：.*$", f"代码ZIP SHA-256：{archive['new_archive_sha256']}", form)
    form_path.write_text(form, encoding="utf-8-sig")

    version_path = team_dir / f"{TEAM_FOLDER}-版本与视频说明.txt"
    version = version_path.read_text(encoding="utf-8-sig")
    version += (
        "\n\n2026-11-13修复版补充：以上旧包哈希不再适用。新版代码与数据ZIP SHA-256："
        f"{archive['new_archive_sha256']}；大小：{archive['new_archive_bytes']}字节。"
        "新版包含界面修复、测试和修复报告。作品方案与答辩PPT仍为11月12日封版，记录423项当时测试；"
        "11月13日补充回归427项、258个子测试通过。新版现场演示视频尚未录制。\n"
    )
    version_path.write_text(version, encoding="utf-8-sig")

    repair_note = (
        "本目录代码与数据ZIP为2026-11-13演示界面修复版。\n"
        f"SHA-256：{archive['new_archive_sha256']}\n"
        f"大小：{archive['new_archive_bytes']}字节\n"
        "回归：427项、258个子测试；历史兼容5项；前端请求恢复5项，全部通过。\n"
        "请停止旧服务后重启：python -X utf8 .\\docs\\demo_v4\\live_v4.py --rules-only；"
        "浏览器访问 http://127.0.0.1:8879/ 并硬刷新。\n"
        "PC/MQTT软件实体仿真，不是真实家居或6G空口。新视频仍需现场录制；03中的视频只是历史备用回放。\n"
    )
    (team_dir / f"{TEAM_FOLDER}-演示界面修复说明.txt").write_text(repair_note, encoding="utf-8")

    for name in ("交付核验结果.json", "提交文件核验.json"):
        path = OUTPUT_SUBMISSION / name
        report = json.loads(path.read_text(encoding="utf-8"))
        report["demo_ui_repair_20261113"] = {
            "code_archive_sha256": archive["new_archive_sha256"],
            "code_archive_bytes": archive["new_archive_bytes"],
            "active_tests_passed": 427,
            "active_subtests_passed": 258,
            "historical_compatibility_tests_passed": 5,
            "dashboard_client_tests_passed": 5,
            "physical_device_or_6g_test": False,
            "new_live_video_present": False,
        }
        write_json(path, report)

    checksum_path = OUTPUT_SUBMISSION / "文件校验SHA256.txt"
    rows = []
    for path in sorted(OUTPUT_SUBMISSION.rglob("*")):
        if path.is_file() and path != checksum_path:
            rel = path.relative_to(OUTPUT_SUBMISSION).as_posix()
            rows.append(f"{sha(path.read_bytes())}  {rel}")
    checksum_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    intro = intro_paths[0].read_text(encoding="utf-8-sig").strip()
    if len(intro) != 300:
        raise RuntimeError(f"Submission intro is {len(intro)} characters, expected 300")
    print(json.dumps({
        "output_directory": str(OUTPUT_SUBMISSION),
        "code_archive": archive,
        "intro_chars": len(intro),
        "new_live_video_present": any(
            file.suffix.lower() == ".mp4"
            for file in (OUTPUT_SUBMISSION / "01_系统直接上传").glob("*")
        ),
        "source_submission_preserved": SOURCE_SUBMISSION.exists(),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
