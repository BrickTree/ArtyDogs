# PyInstaller build: `python -m PyInstaller wardogs_arty.spec` -> dist/WARDOGS-Arty/
# A folder build (not one-file): it starts faster and antivirus flags it far less often.
import importlib.util

from PyInstaller.utils.hooks import collect_data_files

# rapidocr_onnxruntime 1.2.3 appends its own folder to sys.path and imports its stages as
# TOP-LEVEL modules by name from config.yaml (importlib.import_module("ch_ppocr_v3_det")).
# PyInstaller can't see that, so the code must be named here, found via that same folder;
# without it the build starts but the OCR engine fails (caught by `--selftest`).
RAPIDOCR_DIR = importlib.util.find_spec("rapidocr_onnxruntime").submodule_search_locations[0]
OCR_STAGES = ["ch_ppocr_v3_det", "ch_ppocr_v3_rec", "ch_ppocr_v2_cls"]

datas = [
    ("data/weapons.json", "data"),
    ("data/height_correction.json", "data"),
    ("data/terrain/bakurani/manifest.json", "data/terrain/bakurani"),
    ("data/terrain/ozeti/manifest.json", "data/terrain/ozeti"),
    ("data/terrain/zestafona/manifest.json", "data/terrain/zestafona"),
    ("THIRD_PARTY_NOTICES.md", "."),
    ("LICENSE", "."),
]
datas += collect_data_files("rapidocr_onnxruntime")  # the PP-OCR models and config.yaml

a = Analysis(
    ["wardogs_arty.pyw"],
    pathex=[RAPIDOCR_DIR],
    datas=datas,
    hiddenimports=OCR_STAGES,
    excludes=["pytest", "IPython", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WARDOGS-Arty",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="WARDOGS-Arty", upx=False)
