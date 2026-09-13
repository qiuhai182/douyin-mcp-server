# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: packages the tray service (tray_server.py) into
# dist/douyin-server/douyin-server.exe
#
# tray_server.py loads web/app.py via importlib at runtime, and web/app.py
# imports the helper scripts in douyin-video/scripts through a runtime
# sys.path insert - static analysis cannot see either. So those files are
# shipped as DATA (unpacked next to the frozen root), where the runtime
# imports them straight from disk. The third-party packages those data
# scripts import (fastapi, pydantic, ...) are NOT reachable by static
# analysis either, so every runtime dependency is collected explicitly below.

from PyInstaller.utils.hooks import collect_submodules

datas = [
    ('web/app.py', 'web'),
    ('web/templates', 'web/templates'),
    ('douyin-video/scripts/*.py', 'douyin-video/scripts'),
    ('douyin-video.png', '.'),
]

# web/app.py is loaded from disk at runtime, so the packages it imports are
# invisible to static analysis. Their submodules are collected explicitly
# (all pure-Python packages - safe to enumerate whole). C-extension or
# hook-managed packages (PIL/requests/pydantic) must NOT be data-collected;
# their bundled hooks handle binaries correctly.
extra_hidden = (
    collect_submodules('fastapi')
    + collect_submodules('starlette')
    + collect_submodules('uvicorn')
    + collect_submodules('ffmpeg')       # ffmpeg-python (audio pipeline)
    + collect_submodules('dashscope')    # ASR provider SDK
    + [
        'pydantic', 'jinja2', 'anyio', 'sniffio', 'h11',
        'cryptography',                  # cookie decryption in profile_fetcher
        'win32crypt',                    # pywin32 cookie decryption
        'playwright',                    # browser fallback (driver via hook)
    ]
)

a = Analysis(
    ['tray_server.py'],
    pathex=['.', 'web', 'douyin-video/scripts'],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # uvicorn dynamically imports these protocol/loop shims
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.protocols.websockets.wsproto_impl',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        # pystray resolves the Win32 backend at runtime
        'pystray._win32',
    ] + extra_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'PyQt5',
        'PyQt6',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='douyin-server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,               # tray app: no console window
    icon='vscode-extension/media/icon.png',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='douyin-server',
)
