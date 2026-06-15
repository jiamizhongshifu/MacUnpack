# MacUnpack

MacUnpack is a small macOS archive extractor focused on split archives and nested archives.

It can be used as a drag-and-drop macOS app or as a Python command-line tool.

## Features

- Extract common archive formats: `.zip`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tar.xz`, `.gz`, `.bz2`, `.xz`
- Extract `.7z` and `.rar` archives through Homebrew backends
- Detect split archive first volumes automatically:
  - `name.7z.001`, `name.7z.002`
  - `name.zip.001`, `name.zip.002`
  - `name.z01`, `name.z02`, `name.zip`
  - `name.part1.rar`, `name.part2.rar`
  - `name.r00`, `name.r01`, `name.rar`
- Recursively extract nested archives, including files with misleading extensions such as `.tmp` when their content is actually a 7z archive
- Detect obvious fake archive extensions, such as an MP4 file renamed to `.zip` or `.7z`, and create a usable `.mp4` copy instead of failing the run
- Try to join raw MP4 byte streams that are split and disguised as archive volumes, such as `video.7z.001` plus `video.7z.002`
- Validate raw MP4 split joins before writing the final `.mp4`; if later volumes are not referenced by the MP4 index, MacUnpack reports the issue and removes the temporary output
- Automatically create a QuickTime-compatible copy for `HEVC/hev1` MP4 files:
  - output name: `*_quicktime.mp4`
  - no re-encoding
  - original file is preserved

## Requirements

macOS with Python 3.

For full 7z, rar, split-volume, and MP4 compatibility support:

```bash
brew install sevenzip unar ffmpeg
```

`sevenzip` provides `7zz`, `unar` improves RAR compatibility, and `ffmpeg` provides `ffprobe/ffmpeg` for MP4 compatibility remuxing.

## Download And Use

Download `MacUnpack.zip` from the GitHub releases page, unzip it, then drag archive files onto `MacUnpack.app`.

The app opens Terminal so progress and errors are visible.

Output is created next to the source archive using the archive name. Existing output folders are not overwritten; MacUnpack creates a numbered folder instead.

## Command Line

```bash
python3 src/mac-unpack.py archive.7z
python3 src/mac-unpack.py archive.7z.001
python3 src/mac-unpack.py archive.part03.rar
python3 src/mac-unpack.py archive.zip -o ./output
python3 src/mac-unpack.py archive.7z.001 --recursive
python3 src/mac-unpack.py archive.7z.001 --no-fix-mp4
```

Encrypted archives:

```bash
python3 src/mac-unpack.py archive.7z -p 'your-password'
```

## Build The App

```bash
./scripts/build_app.sh
```

The app is written to:

```text
dist/MacUnpack.app
```

Create a distributable zip:

```bash
./scripts/package.sh
```

The package is written to:

```text
dist/MacUnpack.zip
```

## Notes

- Split-volume files must be in the same directory.
- For split archives, you can drag any volume. MacUnpack switches to the first volume automatically.
- If a volume number is missing, MacUnpack fails early.
- Recursive extraction is enabled by default in `MacUnpack.app`; for CLI usage add `--recursive`.
- MP4 QuickTime compatibility remuxing is enabled by default. Use `--no-fix-mp4` to skip it.
- Raw MP4 files disguised as split archives are only recoverable when the MP4 index references bytes from later volumes. If the index only points into the first volume, MacUnpack cannot create a valid combined MP4 from those files.

## License

MIT
