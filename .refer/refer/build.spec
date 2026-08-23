# -*- mode: python ; coding: utf-8 -*-
"""Phigros 资源发布台 PyInstaller 打包配置（单文件 onefile 窗口程序）。

产出：dist/PhigrosResourcePublisher.exe
"""

from PyInstaller.utils.hooks import collect_all

unity_datas, unity_binaries, unity_hidden = collect_all("UnityPy")
pil_datas, pil_binaries, pil_hidden = collect_all("PIL")
boto_datas, boto_binaries, boto_hidden = collect_all("boto3")
botocore_datas, botocore_binaries, botocore_hidden = collect_all("botocore")
# UnityPy 依赖链需要 archspec 的 json/cpu/*.json，漏打会在解包时 FileNotFoundError。
archspec_datas, archspec_binaries, archspec_hidden = collect_all("archspec")
# 音乐提取依赖 fsb5（纯 Python，但 vorbis 重建入口由 rebuild_sample 惰性导入）。
fsb5_datas, fsb5_binaries, fsb5_hidden = collect_all("fsb5")

datas = []
datas += unity_datas
datas += pil_datas
datas += boto_datas
datas += botocore_datas
datas += archspec_datas
datas += fsb5_datas
datas += [("bundled/phiTool", "bundled/phiTool")]

binaries = []
binaries += unity_binaries
binaries += pil_binaries
binaries += boto_binaries
binaries += botocore_binaries
binaries += archspec_binaries
binaries += fsb5_binaries

hiddenimports = []
hiddenimports += unity_hidden
hiddenimports += pil_hidden
hiddenimports += boto_hidden
hiddenimports += botocore_hidden
hiddenimports += archspec_hidden
hiddenimports += fsb5_hidden
hiddenimports += [
    "phigros_publisher",
    "phigros_publisher.apk",
    "phigros_publisher.chart_notes",
    "phigros_publisher.config_store",
    "phigros_publisher.extractor",
    "phigros_publisher.extract_cli",
    "phigros_publisher.organizer",
    "phigros_publisher.paths",
    "phigros_publisher.pipeline",
    "phigros_publisher.state",
    "phigros_publisher.taptap",
    "phigros_publisher.toolchain",
    "phigros_publisher.uploader",
    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.ttk",
]

excludes = [
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "IPython",
    "notebook",
    "jupyter",
    "pytest",
]

a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PhigrosResourcePublisher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
