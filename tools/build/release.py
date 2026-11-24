"""Pack a release from the player's folder (build/dist/<target>, after build.py --dist --runtime-only and dist.py): the
Linux AppImage or the Windows zip. Only the runtime, its libraries and setup/ go in, never anything of the game: a dev
folder that also holds the built game (mkwgame.*, spirv/, the caches) packs the same release.

Linux: run as an AppImage (mounted read-only, APPIMAGE set), the AppRun keeps a copy of mkw in
${XDG_DATA_HOME:-~/.local/share}/mkw-recomp, updated when the AppImage brings a new one, links setup/ there to the mount
(a new path on every start) and starts it with the bundled libraries first on the library path. Unpacked into a folder
it can write to (--appimage-extract, or a launcher such as RecompLauncher, which unpacks every AppImage it installs), it
starts mkw where it is, like the Windows zip: one folder holds the game the setup builds, its settings and saves, however
it was started. mkw also finds the bundled libraries by itself (an rpath of $ORIGIN/lib, tools/build/build.py), for a
launcher that starts it directly. The runtime keeps using the folder of its executable for the game the setup builds,
its settings and caches, as in the Windows zip; the read-only mount only holds the tools. The libraries come from the
sysroot the release links against (tools/build/sysroot.py), which lets it start on systems as old as Debian 11. The
packing follows totk-pc-recomp's tools/make_release_linux.py (MIT): appimagetool and the static type-2 runtime from
release tags (no libfuse2 needed on the player's system), pinned by sha256.

AArch64 (--target linux-aarch64): the same AppImage from build/dist/linux-aarch64 and the Debian 11 arm64 sysroot
(sysroot.py --arch aarch64), packed with this machine's appimagetool and the aarch64 type-2 runtime.

Usage: uv run tools/build/release.py --target linux|linux-aarch64|windows --version 0.1.0
       writes build/release/mkw-recomp-<version>-x86_64.AppImage, -aarch64.AppImage or -windows-x64.zip
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dist import WIN_DLLS, fetch  # noqa: E402
import sysroot  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "build/release"
PINS = {
    "appimagetool": ("https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage",
                     "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"),
    "runtime": ("https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64",
                "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"),
    "runtime-aarch64": ("https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-aarch64",
                        "00cbdfcf917cc6c0ff6d3347d59e0ca1f7f45a6df1a428a0d6d8a78664d87444"),
}  # fmt: skip
# Libraries every desktop has and the AppImage must take from it: glibc, the Vulkan loader (it finds the system's
# drivers), libgcc_s and libstdc++ (the sysroot's is older than any desktop's, and Mesa's drivers need the newer one).
SYSTEM_LIBS = {"libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1", "ld-linux-x86-64.so.2",
               "ld-linux-aarch64.so.1", "libvulkan.so.1", "libgcc_s.so.1", "libstdc++.so.6"}  # fmt: skip
# The licence files of the libraries bundled (the sysroot's, Debian's copyright files) and of the static zstd.
LICENSES = {"SDL3": "usr/local/share/doc/SDL3/LICENSE.txt", "libopus": "usr/share/doc/libopus0/copyright",
            "libvpx": "usr/share/doc/libvpx6/copyright", "zstd": "usr/local/share/doc/zstd/LICENSE"}  # fmt: skip
GAME_MARKER = "MKW_RECOMP_" + "GENERATED_FROM_GAME_CODE"
# The licences of code built into the runtime and of the libraries the settings download (libs.tsv), in licenses/ of
# both packages: FidelityFX (its shader in the runtime, FSR 3.1's DLLs), RenHDrum (its C bridge, its DLL) and the
# notice naming each downloaded library, NVIDIA DLSS's attribution included (its SDK licence, RTX supplement 7.1b).
THIRD_PARTY = {"FidelityFX-LICENSE.txt": "runtime/third_party/ffx_sdk/LICENSE.txt",
               "RenHDrum-LICENSE-MIT": "runtime/input/renhdrum_LICENSE-MIT",
               "THIRD_PARTY_NOTICES.txt": "tools/build/THIRD_PARTY_NOTICES.txt"}  # fmt: skip

APPRUN = """#!/bin/sh
# The AppImage's entry (tools/build/release.py). Mounted (APPIMAGE set, read-only), the runtime runs from a copy in the
# player's data folder, which holds the game the setup builds; setup/ and the libraries stay in the mount. Unpacked
# into a folder it can write to, it runs where it is, so that folder holds everything however it is started.
set -eu
here=$(dirname "$(readlink -f "$0")")
export LD_LIBRARY_PATH="$here/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
if [ "${RECOMPLAUNCHER:-}" = 1 ] && [ "${1:-}" = --recomplauncher ]; then
    # RecompLauncher's setup step (Easy Setup): this package's tools, in this folder
    exec "$here/setup/python/bin/python3" -I "$here/setup/src/tools/prepare/prepare.py" --runtime "$here" "$@"
fi
if [ "${RECOMPLAUNCHER:-}" = 1 ] || { [ -z "${APPIMAGE:-}" ] && [ -w "$here" ]; }; then
    # Unpacked: by RecompLauncher (it unpacks every AppImage it installs, and never sets APPIMAGE) or by
    # --appimage-extract. Not --appimage-extract-and-run: its folder goes when the game closes, and it sets APPIMAGE.
    exec "$here/mkw" "$@"
fi
if [ "${1:-}" = --prepare-next ]; then
    # the start screen of the version still played sets this one up beside it (prepare.py --next): this package's
    # tools, and no copy of its mkw over the one running
    shift
    exec "$here/setup/python/bin/python3" -I "$here/setup/src/tools/prepare/prepare.py" --runtime "$here" --next "$@"
fi
home=${XDG_DATA_HOME:-$HOME/.local/share}/mkw-recomp
mkdir -p "$home"
if ! cmp -s "$here/mkw" "$home/mkw"; then
    # a rename, not a write over it: a mkw still running keeps its old file
    cp "$here/mkw" "$home/mkw.new"
    mv -f "$home/mkw.new" "$home/mkw"
fi
ln -sfn "$here/setup" "$home/setup"
exec "$home/mkw" "$@"
"""

DESKTOP = """[Desktop Entry]
Type=Application
Name=mkw-recomp
Comment=Static recompilation of Mario Kart World; builds the game from your own copy
Exec=AppRun
Icon=mkw-recomp
Categories=Game;
Terminal=false
"""


def needed(elf: Path) -> list[str]:
    out = subprocess.run(["readelf", "-d", str(elf)], capture_output=True, text=True, check=True).stdout
    return [line.split("[", 1)[1].rstrip("]") for line in out.splitlines() if "(NEEDED)" in line]


def find_lib(name: str) -> Path:
    root, triple = sysroot.SYSROOT, sysroot.TRIPLE
    for d in ("usr/local/lib", f"usr/lib/{triple}", f"lib/{triple}"):
        if (root / d / name).exists():
            return (root / d / name).resolve()
    sys.exit(f"{name}: not in {root} (built with build.py --sysroot?)")


def bundle_libs(exe: Path, lib: Path) -> None:
    """The libraries mkw needs that are not SYSTEM_LIBS, and theirs, from the sysroot."""
    lib.mkdir()
    todo = [n for n in needed(exe) if n not in SYSTEM_LIBS]
    while todo:
        name = todo.pop()
        if (lib / name).exists():
            continue
        src = find_lib(name)
        shutil.copy2(src, lib / name)
        todo += [n for n in needed(src) if n not in SYSTEM_LIBS]
    print("bundled:", " ".join(sorted(p.name for p in lib.iterdir())))


def check_no_game(root: Path) -> None:
    """The generated C carries GAME_MARKER (recomp/gen.py); setup/src is the repository's sources. NVIDIA's SM86 spec
    neither: the player's setup builds it (sm86/spec/spec.py)."""
    for p in root.rglob("*"):
        if p.name in ("instructions.json", "sm_86_instructions.txt"):
            sys.exit(f"{p}: NVIDIA's SM86 spec in the release")
        if p.is_file() and not p.is_symlink() and p.name != "gen.py" and GAME_MARKER.encode() in p.read_bytes():
            sys.exit(f"{p}: game code in the release")


def licenses(dst: Path) -> None:
    dst.mkdir()
    shutil.copy2(ROOT / "LICENSE", dst / "mkw-recomp-LICENSE")
    for name, rel in THIRD_PARTY.items():
        shutil.copy2(ROOT / rel, dst / name)
    for name, rel in LICENSES.items():
        shutil.copy2(sysroot.SYSROOT / rel, dst / f"{name}-copyright")


def launcher_manifest(version: str, *, windows: bool) -> str:
    manifest = json.loads((ROOT / "tools/build/recomplauncher.json").read_text(encoding="utf-8"))
    prepare = manifest["prepare"]
    if not windows:
        manifest["start"]["program"] = ["AppRun"]
        prepare["program"] = ["AppRun"]
        prepare["args"] = prepare["args"][2:]  # AppRun supplies the bundled Python and script.
        prepare["done"] = [path.removesuffix(".dll") + ".so" if path.endswith(".dll") else path
                           for path in prepare["done"]]
    else:
        # mkw.exe (to unpack a .nup/.nx and to warm the shaders) is in the install folder, not in {data}: without this
        # prepare.py looks for it in the game folder. On Linux AppRun passes --runtime itself.
        prepare["args"] += ["--runtime", "{install}"]
    prepare["args"] += ["--port", version]
    return json.dumps(manifest, indent=2) + "\n"


def linux(dist: Path, version: str, arch: str = "x86_64") -> Path:
    exe = dist / "mkw"
    glibc = subprocess.run(["objdump", "-T", str(exe)], capture_output=True, text=True, check=True).stdout
    newest = max((tuple(map(int, v.split("."))) for v in set(_glibc_versions(glibc))), default=(0, 0))
    if newest > (2, 31):  # built against the machine's glibc: the AppImage would need that glibc or newer
        sys.exit(f"{exe} needs glibc {'.'.join(map(str, newest))}: build.py --sysroot {sysroot.SYSROOT}")
    app = OUT / "AppDir"
    shutil.rmtree(app, ignore_errors=True)
    app.mkdir(parents=True)
    shutil.copy2(exe, app / "mkw")
    (app / "recomplauncher.json").write_text(launcher_manifest(version, windows=False), encoding="utf-8")
    shutil.copytree(dist / "setup", app / "setup", symlinks=True)
    bundle_libs(exe, app / "lib")
    licenses(app / "licenses")
    check_no_game(app)
    (app / "AppRun").write_text(APPRUN)
    (app / "AppRun").chmod(0o755)
    (app / "mkw-recomp.desktop").write_text(DESKTOP)
    subprocess.run([sys.executable, str(ROOT / "tools/build/icon.py"), str(app / "mkw-recomp.png")], check=True)
    (app / ".DirIcon").symlink_to("mkw-recomp.png")
    # this machine's appimagetool packs either architecture; the runtime is the target's
    tool, runtime = fetch(*PINS["appimagetool"]), fetch(*PINS["runtime" if arch == "x86_64" else f"runtime-{arch}"])
    tool.chmod(0o755)
    out = OUT / f"mkw-recomp-{version}-{arch}.AppImage"
    # appimagetool is an AppImage itself: extracted and run, no FUSE in a container. Into a .part then renamed: an
    # AppImage of the same name still mounted (a test of it) made mksquashfs fail with "Text file busy" (09/11/2026).
    env = dict(os.environ, ARCH=arch, APPIMAGE_EXTRACT_AND_RUN="1")
    part = out.with_name(out.name + ".part")
    subprocess.run([str(tool), "--no-appstream", "--runtime-file", str(runtime), str(app), str(part)], env=env,
                   check=True)  # fmt: skip
    part.replace(out)
    shutil.rmtree(app)
    return out


def _glibc_versions(text: str) -> list[str]:
    return [w.removeprefix("(GLIBC_").rstrip(")") for w in text.split() if w.startswith("(GLIBC_2.")]


def windows(dist: Path, version: str, noavx2: bool = False) -> Path:
    """The Windows zip. noavx2: the package for CPUs without AVX2 (#82): the same setup/ and DLLs, mkw.exe built for
    x86-64-v2 (build/dist/windows-noavx2) and setup/cpu-level naming v2, so the setup compiles the game code for v2 and
    the updater keeps the player on this package."""
    out = OUT / f"mkw-recomp-{version}-windows-x64{'-noavx2' if noavx2 else ''}.zip"
    exe = ROOT / "build/dist/windows-noavx2/mkw.exe" if noavx2 else dist / "mkw.exe"
    files = [exe, *(dist / d for d in WIN_DLLS), *sorted((dist / "setup").rglob("*"))]
    check_no_game(dist / "setup")
    top = Path("mkw-recomp")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in files:
            if p.is_file():
                z.write(p, top / p.relative_to(dist if p.is_relative_to(dist) else exe.parent))
        if noavx2:
            z.writestr(str(top / "setup/cpu-level"), "v2\n")
        z.write(ROOT / "LICENSE", top / "licenses/mkw-recomp-LICENSE")
        for name, rel in THIRD_PARTY.items():
            z.write(ROOT / rel, top / "licenses" / name)
        z.writestr((top / "recomplauncher.json").as_posix(), launcher_manifest(version, windows=True))
        # the libraries in mkw.exe and beside it (tools/build/windeps.py keeps each package's licence files)
        for p in sorted((ROOT / "build/win/deps/share/licenses").rglob("*")):
            if p.is_file():
                z.write(p, top / "licenses" / p.relative_to(ROOT / "build/win/deps/share/licenses"))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", choices=("linux", "linux-aarch64", "windows"), default="linux")
    ap.add_argument("--version", required=True, help="the release's version, e.g. 0.1.0 (the tag without its v)")
    ap.add_argument("--cpu", choices=("v3", "noavx2"), default="v3", help="windows: noavx2 = the package for CPUs without AVX2")
    args = ap.parse_args()
    if args.cpu == "noavx2" and args.target != "windows":
        ap.error("--cpu noavx2 is only for --target windows so far")
    dist = ROOT / "build/dist" / args.target
    if not (dist / "setup").is_dir():
        sys.exit(f"{dist}/setup missing: uv run tools/build/dist.py --target {args.target} first")
    OUT.mkdir(parents=True, exist_ok=True)
    if args.target == "windows":
        out = windows(dist, args.version, args.cpu == "noavx2")
    else:
        arch = "aarch64" if args.target == "linux-aarch64" else "x86_64"
        sysroot.use_arch(arch)
        out = linux(dist, args.version, arch)
    print(f"{out}: {out.stat().st_size / 2**20:.0f} MiB")


if __name__ == "__main__":
    main()
