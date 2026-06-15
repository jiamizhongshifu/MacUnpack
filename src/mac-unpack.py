#!/usr/bin/env python3
"""
mac-unpack: a small macOS archive extractor with split-volume awareness.

It intentionally uses the tools that are best suited to each archive family:
- built-in Python/Apple tools for zip, tar, gz, bz2, and xz
- 7zz/7z, unar, or unrar for 7z/rar and split archives
"""

from __future__ import annotations

import argparse
import bz2
import gzip
import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


EXTRA_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
EXIT_OK = 0
EXIT_PARTIAL = 2
EXIT_ERROR = 1


@dataclass(frozen=True)
class ArchivePlan:
    source: Path
    first_volume: Path
    family: str
    display_name: str
    volume_paths: tuple[Path, ...]
    warnings: tuple[str, ...] = ()


class UserError(Exception):
    pass


def main() -> int:
    args = parse_args()

    if args.install_hint:
        print_install_hint()
        return EXIT_OK

    if not args.paths:
        print("No archives supplied.\n")
        print_usage_examples()
        return EXIT_ERROR

    status = EXIT_OK
    for raw_path in args.paths:
        source = Path(raw_path).expanduser().resolve()
        out_dir: Path | None = None
        try:
            plan = build_plan(source)
            preflight_backend(plan)
            for warning in plan.warnings:
                print(f"[warn] {warning}")
            out_dir = resolve_output_dir(plan, args.output, len(args.paths))
            out_dir.mkdir(parents=True, exist_ok=False)
            print(f"[extract] {plan.display_name}")
            print(f"[target]  {out_dir}")
            extract(plan, out_dir, args.password, args.overwrite)
            if args.recursive:
                extract_nested_archives(out_dir, args.password, args.overwrite)
            if args.fix_mp4:
                fix_quicktime_mp4s(out_dir)
            print(f"[done]    {out_dir}\n")
        except Exception as exc:
            status = EXIT_PARTIAL if len(args.paths) > 1 else EXIT_ERROR
            print(f"[error]   {source}: {exc}\n", file=sys.stderr)
            if out_dir and not args.keep_failed and not args.output:
                shutil.rmtree(out_dir, ignore_errors=True)
    return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mac-unpack",
        description="Extract common archive formats on macOS, with split-volume detection.",
    )
    parser.add_argument("paths", nargs="*", help="Archive files or split-volume files to extract.")
    parser.add_argument(
        "-o",
        "--output",
        help="Output directory. With one archive this is used directly; with multiple archives, each gets a subfolder.",
    )
    parser.add_argument("-p", "--password", help="Password for encrypted 7z/rar/zip archives.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow backend tools to overwrite files inside the output directory.",
    )
    parser.add_argument(
        "--keep-failed",
        action="store_true",
        help="Keep an output directory if extraction fails.",
    )
    parser.add_argument(
        "--install-hint",
        action="store_true",
        help="Show recommended backend install commands.",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Keep extracting nested archives found inside the output directory.",
    )
    parser.add_argument(
        "--no-fix-mp4",
        action="store_false",
        dest="fix_mp4",
        help="Skip automatic QuickTime-compatible HEVC MP4 remuxing.",
    )
    parser.set_defaults(fix_mp4=True)
    return parser.parse_args()


def print_usage_examples() -> None:
    print("Examples:")
    print("  ./mac-unpack.py archive.7z")
    print("  ./mac-unpack.py movie.7z.001")
    print("  ./mac-unpack.py backup.part03.rar")
    print("  ./mac-unpack.py file.zip -o ./extracted")
    print("  ./mac-unpack.py archive.7z.001 --recursive")
    print("  ./mac-unpack.py archive.7z.001 --no-fix-mp4")
    print("  ./mac-unpack.py --install-hint")


def print_install_hint() -> None:
    print("Recommended install for full 7z/rar/split-volume support:")
    print("  brew install sevenzip unar")


def detect_obvious_mismatch(source: Path) -> str | None:
    try:
        with source.open("rb") as handle:
            header = handle.read(64)
    except OSError:
        return None

    lower = source.name.lower()
    archive_like = lower.endswith(
        (
            ".7z",
            ".7z.001",
            ".rar",
            ".zip",
            ".tar",
            ".tar.gz",
            ".tgz",
            ".tar.bz2",
            ".tbz2",
            ".tar.xz",
            ".txz",
        )
    )
    if not archive_like:
        return None

    if len(header) >= 12 and header[4:8] == b"ftyp":
        return (
            "file extension looks like an archive, but the file content is an MP4 video. "
            "Rename it to .mp4; it does not need extraction."
        )
    if header.startswith(b"<!DOCTYPE html") or header.startswith(b"<html") or b"<html" in header[:32].lower():
        return (
            "file extension looks like an archive, but the file content is HTML. "
            "This is likely a downloaded web page, not the real archive."
        )
    return None


def detect_archive_magic(source: Path) -> tuple[str, str] | None:
    try:
        with source.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        return None

    if header.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ("7z", ".7z")
    if header.startswith(b"Rar!\x1a\x07"):
        return ("rar", ".rar")
    if header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return ("zip", ".zip")
    if header.startswith(b"\x1f\x8b"):
        return ("single-gzip", ".gz")
    if header.startswith(b"BZh"):
        return ("single-bzip2", ".bz2")
    if header.startswith(b"\xfd7zXZ\x00"):
        return ("single-xz", ".xz")
    return None


def build_plan(source: Path) -> ArchivePlan:
    if not source.exists():
        raise UserError("file does not exist")
    if source.is_dir():
        raise UserError("directories are not archives")
    mismatch = detect_obvious_mismatch(source)
    if mismatch:
        raise UserError(mismatch)

    name = source.name
    lower = name.lower()
    parent = source.parent

    split_7z = re.match(r"^(?P<base>.+\.(?:7z|zip))\.(?P<num>\d{3})$", name, re.I)
    if split_7z:
        base = split_7z.group("base")
        first = parent / f"{base}.001"
        volumes, warnings = collect_numeric_volumes(parent, base, 3)
        family = "7z" if base.lower().endswith(".7z") else "zip-split"
        return ArchivePlan(source, first, family, strip_suffixes(base, [".7z", ".zip"]), volumes, warnings)

    z_part = re.match(r"^(?P<base>.+)\.z(?P<num>\d{2})$", name, re.I)
    if z_part:
        first = parent / f"{z_part.group('base')}.zip"
        volumes, warnings = collect_zip_parts(parent, z_part.group("base"))
        warnings = warnings + (f"using ZIP central-directory file instead of selected volume: {first.name}",)
        return ArchivePlan(source, first, "zip-split", z_part.group("base"), volumes, warnings)

    rar_part = re.match(r"^(?P<base>.+)\.part(?P<num>\d+)\.rar$", name, re.I)
    if rar_part:
        width = len(rar_part.group("num"))
        first_num = "1".zfill(width)
        first = parent / f"{rar_part.group('base')}.part{first_num}.rar"
        volumes, warnings = collect_rar_part_volumes(parent, rar_part.group("base"), width)
        warnings = warnings + maybe_selected_later_volume_warning(source, first)
        return ArchivePlan(source, first, "rar", rar_part.group("base"), volumes, warnings)

    rar_legacy = re.match(r"^(?P<base>.+)\.r(?P<num>\d{2})$", name, re.I)
    if rar_legacy:
        first = parent / f"{rar_legacy.group('base')}.rar"
        volumes, warnings = collect_legacy_rar_volumes(parent, rar_legacy.group("base"))
        warnings = warnings + (f"using first RAR volume instead of selected volume: {first.name}",)
        return ArchivePlan(source, first, "rar", rar_legacy.group("base"), volumes, warnings)

    if lower.endswith(".part1.rar") or lower.endswith(".part01.rar"):
        base = re.sub(r"\.part0*1\.rar$", "", name, flags=re.I)
        width = len(re.search(r"\.part(\d+)\.rar$", name, re.I).group(1))  # type: ignore[union-attr]
        volumes, warnings = collect_rar_part_volumes(parent, base, width)
        return ArchivePlan(source, source, "rar", base, volumes, warnings)

    archive_suffixes = [
        (".tar.gz", "tar", ".tar.gz"),
        (".tgz", "tar", ".tgz"),
        (".tar.bz2", "tar", ".tar.bz2"),
        (".tbz2", "tar", ".tbz2"),
        (".tar.xz", "tar", ".tar.xz"),
        (".txz", "tar", ".txz"),
        (".tar", "tar", ".tar"),
        (".zip", "zip", ".zip"),
        (".7z", "7z", ".7z"),
        (".rar", "rar", ".rar"),
        (".gz", "single-gzip", ".gz"),
        (".bz2", "single-bzip2", ".bz2"),
        (".xz", "single-xz", ".xz"),
    ]
    for suffix, family, display_suffix in archive_suffixes:
        if lower.endswith(suffix):
            return ArchivePlan(source, source, family, name[: -len(display_suffix)], (source,))

    magic = detect_archive_magic(source)
    if magic:
        family, _ = magic
        return ArchivePlan(source, source, family, source.stem, (source,), ("archive type detected from file content, not extension",))

    raise UserError("unsupported archive extension")


def collect_numeric_volumes(parent: Path, base: str, width: int) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    pattern = re.compile(rf"^{re.escape(base)}\.(\d{{{width}}})$", re.I)
    numbered = sorted(
        ((int(match.group(1)), path) for path in parent.iterdir() if (match := pattern.match(path.name))),
        key=lambda item: item[0],
    )
    if not numbered:
        raise UserError(f"first split volume not found: {base}.{'1'.zfill(width)}")
    first_expected = 1
    if numbered[0][0] != first_expected:
        raise UserError(f"first split volume not found: {base}.{'1'.zfill(width)}")
    gaps = find_numeric_gaps(numbered)
    if gaps:
        raise UserError(gaps[0])
    return tuple(path for _, path in numbered), ()


def collect_zip_parts(parent: Path, base: str) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    pattern = re.compile(rf"^{re.escape(base)}\.z(\d{{2}})$", re.I)
    numbered = sorted(
        ((int(match.group(1)), path) for path in parent.iterdir() if (match := pattern.match(path.name))),
        key=lambda item: item[0],
    )
    zip_file = parent / f"{base}.zip"
    volumes = [path for _, path in numbered]
    if zip_file.exists():
        volumes.append(zip_file)
    else:
        raise UserError(f"ZIP central-directory file not found: {zip_file.name}")
    if numbered:
        gaps = find_numeric_gaps(numbered)
        if gaps:
            raise UserError(gaps[0])
    return tuple(volumes), ()


def collect_rar_part_volumes(parent: Path, base: str, width: int) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    pattern = re.compile(rf"^{re.escape(base)}\.part(\d+)\.rar$", re.I)
    numbered = sorted(
        ((int(match.group(1)), path) for path in parent.iterdir() if (match := pattern.match(path.name))),
        key=lambda item: item[0],
    )
    if not numbered:
        raise UserError(f"RAR volumes not found for: {base}")
    if numbered[0][0] != 1:
        raise UserError(f"first RAR volume not found: {base}.part{'1'.zfill(width)}.rar")
    gaps = find_numeric_gaps(numbered)
    if gaps:
        raise UserError(gaps[0])
    return tuple(path for _, path in numbered), ()


def collect_legacy_rar_volumes(parent: Path, base: str) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    first = parent / f"{base}.rar"
    numbered = [(0, first)] if first.exists() else []
    pattern = re.compile(rf"^{re.escape(base)}\.r(\d{{2}})$", re.I)
    numbered.extend(
        sorted(
            ((int(match.group(1)) + 1, path) for path in parent.iterdir() if (match := pattern.match(path.name))),
            key=lambda item: item[0],
        )
    )
    if not numbered:
        raise UserError(f"legacy RAR first volume not found: {first.name}")
    if numbered[0][0] != 0:
        raise UserError(f"legacy RAR first volume not found: {first.name}")
    gaps = find_numeric_gaps(numbered)
    if gaps:
        raise UserError(gaps[0])
    return tuple(path for _, path in numbered), ()


def find_numeric_gaps(numbered: list[tuple[int, Path]]) -> tuple[str, ...]:
    if not numbered:
        return ()
    present = {num for num, _ in numbered}
    missing = [str(num) for num in range(min(present), max(present) + 1) if num not in present]
    if missing:
        return (f"possible missing split volumes: {', '.join(missing)}",)
    return ()


def maybe_selected_later_volume_warning(source: Path, first: Path) -> tuple[str, ...]:
    if source.name != first.name:
        return (f"using first RAR volume instead of selected volume: {first.name}",)
    return ()


def resolve_output_dir(plan: ArchivePlan, explicit_output: str | None, archive_count: int) -> Path:
    if explicit_output:
        target = Path(explicit_output).expanduser().resolve()
        if target.exists() and not target.is_dir():
            raise UserError(f"output path is not a directory: {target}")
        if archive_count > 1:
            target = target / plan.display_name
        return unique_path(target)

    base = plan.display_name.strip() or plan.first_volume.stem
    target = plan.first_volume.parent / base
    return unique_path(target)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.name}-{index}")
        if not candidate.exists():
            return candidate
    raise UserError(f"could not find available output directory near: {path}")


def extract(plan: ArchivePlan, out_dir: Path, password: str | None, overwrite: bool) -> None:
    if plan.family == "tar":
        extract_tar(plan.first_volume, out_dir)
    elif plan.family == "zip":
        extract_zip(plan.first_volume, out_dir, password)
    elif plan.family == "zip-split":
        extract_with_external(plan, out_dir, password, overwrite, prefer=("7zz", "7z", "unar"))
    elif plan.family == "7z":
        extract_with_external(plan, out_dir, password, overwrite, prefer=("7zz", "7z", "unar"))
    elif plan.family == "rar":
        extract_with_external(plan, out_dir, password, overwrite, prefer=("unar", "7zz", "7z", "unrar"))
    elif plan.family == "single-gzip":
        decompress_single(plan.first_volume, out_dir, gzip.open, ".gz")
    elif plan.family == "single-bzip2":
        decompress_single(plan.first_volume, out_dir, bz2.open, ".bz2")
    elif plan.family == "single-xz":
        decompress_single(plan.first_volume, out_dir, lzma.open, ".xz")
    else:
        raise UserError(f"unsupported archive family: {plan.family}")


def extract_nested_archives(root: Path, password: str | None, overwrite: bool, max_depth: int = 5) -> None:
    processed: set[Path] = set()
    for _depth in range(max_depth):
        candidates = [
            path
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.resolve() not in processed and looks_like_nested_archive(path)
        ]
        if not candidates:
            return

        extracted_any = False
        for source in candidates:
            processed.add(source.resolve())
            try:
                plan = build_plan(source)
                preflight_backend(plan)
                target = unique_path(source.parent / plan.display_name)
                target.mkdir(parents=True, exist_ok=False)
                print(f"[nested]  {source}")
                print(f"[target]  {target}")
                extract(plan, target, password, overwrite)
                extracted_any = True
            except Exception as exc:
                print(f"[warn]    nested archive skipped: {source}: {exc}")

        if not extracted_any:
            return

    print(f"[warn]    recursive extraction stopped after {max_depth} levels")


def looks_like_nested_archive(source: Path) -> bool:
    lower = source.name.lower()
    if lower.endswith((".7z", ".rar", ".zip", ".tar", ".tar.gz", ".tgz", ".gz", ".bz2", ".xz")):
        return True
    return detect_archive_magic(source) is not None


def fix_quicktime_mp4s(root: Path) -> None:
    mp4s = [
        path
        for path in sorted(root.rglob("*.mp4"))
        if path.is_file() and not is_generated_mp4_variant(path)
    ]
    if not mp4s:
        return

    ffprobe = first_available(("ffprobe",))
    ffmpeg = first_available(("ffmpeg",))
    if not ffprobe or not ffmpeg:
        print("[warn]    ffmpeg/ffprobe not found; skipped automatic MP4 compatibility fix")
        return

    for source in mp4s:
        try:
            if not needs_quicktime_hevc_fix(source, ffprobe):
                continue
            target = source.with_name(f"{source.stem}_quicktime{source.suffix}")
            if target.exists():
                print(f"[mp4]     QuickTime-compatible file already exists: {target}")
                continue
            print(f"[mp4]     creating QuickTime-compatible copy: {target}")
            remux_for_quicktime(source, target, ffmpeg)
        except Exception as exc:
            print(f"[warn]    MP4 compatibility fix skipped: {source}: {exc}")


def needs_quicktime_hevc_fix(source: Path, ffprobe: str) -> bool:
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,codec_tag_string",
        "-of",
        "json",
        str(source),
    ]
    result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        return False

    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        return False

    stream = streams[0]
    return stream.get("codec_name") == "hevc" and stream.get("codec_tag_string") == "hev1"


def is_generated_mp4_variant(source: Path) -> bool:
    return source.stem.endswith(("_quicktime", "_fixed", "_fixed_quicktime"))


def remux_for_quicktime(source: Path, target: Path, ffmpeg: str) -> None:
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
        "-c",
        "copy",
        "-tag:v",
        "hvc1",
        "-movflags",
        "+faststart",
        str(target),
    ]
    result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        if target.exists():
            target.unlink()
        raise UserError(f"ffmpeg failed with exit code {result.returncode}\n{last_output_lines(result.stdout)}")


def preflight_backend(plan: ArchivePlan) -> None:
    if plan.family in ("zip-split", "7z") and not first_available(("7zz", "7z", "unar")):
        raise UserError(
            f"{plan.family} requires an external backend. Run `brew install sevenzip` "
            "then retry. Optional fallback: `brew install unar`."
        )
    if plan.family == "rar" and not first_available(("7zz", "7z", "unar", "unrar")):
        raise UserError(
            "rar requires an external backend. Run `brew install sevenzip` "
            "then retry. Optional fallback: `brew install unar`."
        )


def extract_tar(source: Path, out_dir: Path) -> None:
    with tarfile.open(source, mode="r:*") as archive:
        safe_extract_tar(archive, out_dir)


def safe_extract_tar(archive: tarfile.TarFile, out_dir: Path) -> None:
    root = out_dir.resolve()
    for member in archive.getmembers():
        destination = (root / member.name).resolve()
        if not str(destination).startswith(str(root) + os.sep) and destination != root:
            raise UserError(f"unsafe path inside tar archive: {member.name}")
    try:
        archive.extractall(root, filter="data")
    except TypeError:
        archive.extractall(root)


def extract_zip(source: Path, out_dir: Path, password: str | None) -> None:
    with zipfile.ZipFile(source) as archive:
        safe_extract_zip(archive, out_dir, password)


def safe_extract_zip(archive: zipfile.ZipFile, out_dir: Path, password: str | None) -> None:
    root = out_dir.resolve()
    pwd = password.encode("utf-8") if password else None
    for member in archive.infolist():
        destination = (root / member.filename).resolve()
        if not str(destination).startswith(str(root) + os.sep) and destination != root:
            raise UserError(f"unsafe path inside zip archive: {member.filename}")
        archive.extract(member, root, pwd=pwd)


def decompress_single(source: Path, out_dir: Path, opener, suffix: str) -> None:
    output_name = source.name[: -len(suffix)] or f"{source.stem}.out"
    destination = out_dir / output_name
    with opener(source, "rb") as reader, destination.open("wb") as writer:
        shutil.copyfileobj(reader, writer)


def extract_with_external(
    plan: ArchivePlan,
    out_dir: Path,
    password: str | None,
    overwrite: bool,
    prefer: Iterable[str],
) -> None:
    backends = available_backends(prefer)
    if not backends:
        raise UserError(
            f"{plan.family} requires an external backend. Run `brew install sevenzip` "
            "then retry. Optional fallback: `brew install unar`."
        )

    errors: list[str] = []
    for backend in backends:
        cmd = build_external_command(backend, plan, out_dir, password, overwrite)
        result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode == 0:
            return
        errors.append(f"{Path(backend).name} exit {result.returncode}: {last_output_lines(result.stdout)}")

    raise UserError("all extraction backends failed\n" + "\n".join(errors))


def build_external_command(
    backend: str,
    plan: ArchivePlan,
    out_dir: Path,
    password: str | None,
    overwrite: bool,
) -> list[str]:
    backend_name = Path(backend).name
    if backend_name in ("7zz", "7z"):
        cmd = [backend, "x", str(plan.first_volume), f"-o{out_dir}", "-y" if overwrite else "-aos"]
        if password is not None:
            cmd.append(f"-p{password}")
    elif backend_name == "unar":
        cmd = [backend, "-output-directory", str(out_dir), "-force-overwrite" if overwrite else "-force-skip"]
        if password is not None:
            cmd.extend(["-password", password])
        cmd.append(str(plan.first_volume))
    elif backend_name == "unrar":
        if plan.family != "rar":
            raise UserError("unrar can only extract RAR archives")
        password_arg = f"-p{password}" if password is not None else "-p-"
        overwrite_arg = "-o+" if overwrite else "-o-"
        cmd = [backend, "x", password_arg, overwrite_arg, str(plan.first_volume), str(out_dir) + os.sep]
    else:
        raise UserError(f"unsupported backend: {backend}")
    return cmd


def first_available(commands: Iterable[str]) -> str | None:
    for command in available_backends(commands):
        return command
    return None


def available_backends(commands: Iterable[str]) -> list[str]:
    found: list[str] = []
    for command in commands:
        path = shutil.which(command)
        if path:
            found.append(path)
            continue
        for tool_dir in EXTRA_TOOL_DIRS:
            candidate = Path(tool_dir) / command
            if candidate.exists() and os.access(candidate, os.X_OK):
                found.append(str(candidate))
                break
    return found


def last_output_lines(output: str, count: int = 8) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return "\n".join(lines[-count:])


def strip_suffixes(name: str, suffixes: list[str]) -> str:
    lower = name.lower()
    for suffix in suffixes:
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return name


if __name__ == "__main__":
    raise SystemExit(main())
