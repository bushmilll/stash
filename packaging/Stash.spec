# PyInstaller recipe for Stash.app. Built by .github/workflows/build-mac.yml on GitHub's Macs.
import os
import imageio_ffmpeg
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
VERSION = os.environ.get("STASH_VERSION", "0.1.0")

hidden = (
    collect_submodules("gallery_dl")      # Instagram support is loaded by name at runtime
    + collect_submodules("uvicorn")
    + collect_submodules("webview")
    + ["app.main", "app.selftest", "multipart", "python_multipart"]
)
datas = [(os.path.join(ROOT, "app", "static"), "app/static")] + collect_data_files("webview")
ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
binaries = [(ffmpeg, "imageio_ffmpeg/binaries")]
for pkg in ("onnxruntime", "tokenizers"):          # on-device AI tags
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h
hidden += ["app.ai", "app.storage"]

a = Analysis(
    [os.path.join(ROOT, "stash_app.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide6", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Stash", console=False,
          argv_emulation=False, codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, name="Stash")
app = BUNDLE(
    coll,
    name="Stash.app",
    icon=os.path.join(ROOT, "packaging", "Stash.icns"),
    bundle_identifier="app.stash.library",
    version=VERSION,
    info_plist={
        "CFBundleName": "Stash",
        "CFBundleDisplayName": "Stash",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        "NSDownloadsFolderUsageDescription": "Stash saves the files and exports you ask for to your Downloads folder.",
        "NSDesktopFolderUsageDescription": "Stash can save exports to your Desktop.",
        "NSDocumentsFolderUsageDescription": "Stash can save exports to your Documents.",
    },
)
