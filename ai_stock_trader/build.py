"""단일 실행 파일 빌드.

    pip install pyinstaller
    python build.py

결과: dist/KIS자동매매.exe  (하나만 나온다)
.env / config / data / logs 는 exe 옆에 생성되므로 exe와 같은 폴더에 .env를 두면 된다.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "KIS자동매매"


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller가 없습니다:  pip install pyinstaller")
        return 1

    for d in ("build", "dist"):
        shutil.rmtree(ROOT / d, ignore_errors=True)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--windowed",                 # 콘솔 창 없이
        "--name", NAME,
        # customtkinter는 테마 json/에셋을 런타임에 읽으므로 통째로 넣어야 한다
        "--collect-data", "customtkinter",
        "--collect-submodules", "customtkinter",
        "--hidden-import", "google.genai",
        "--hidden-import", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "pandas",
        "--exclude-module", "numpy",
        "--exclude-module", "yfinance",
        "--exclude-module", "PyQt5",
        "--exclude-module", "PySide2",
        str(ROOT / "run.py"),
    ]
    icon = ROOT / "app" / "icon.ico"
    if icon.exists():
        args[-1:-1] = ["--icon", str(icon)]     # run.py 앞에 끼워넣는다

    print("빌드 시작...\n  " + " ".join(args[2:]))
    r = subprocess.run(args, cwd=ROOT)
    if r.returncode != 0:
        return r.returncode

    exe = ROOT / "dist" / f"{NAME}.exe"
    if not exe.exists():
        print("빌드는 끝났는데 exe를 찾을 수 없습니다.")
        return 1

    # 이미 .env / config / data / logs 가 있는 프로젝트 폴더로 옮긴다.
    # (exe는 자기 옆 폴더를 설정/DB 위치로 쓰므로 여기 두면 기존 데이터를 그대로 이어받는다)
    final = ROOT / f"{NAME}.exe"
    try:
        shutil.copy2(exe, final)
    except PermissionError:
        print(f"\n{final} 이 실행 중입니다. 프로그램을 닫고 다시 빌드하세요.")
        return 1
    print(f"\n완료: {final}  ({final.stat().st_size / 1024 / 1024:.1f} MB)")

    if not (ROOT / ".env").exists():
        print("\n[주의] .env가 없습니다. .env.example을 복사해 키를 채워주세요.")

    make_shortcut(final)
    return 0


def make_shortcut(target: Path) -> None:
    """바탕화면 바로가기 생성 (실패해도 빌드는 성공)."""
    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        return
    lnk = desktop / f"{NAME}.lnk"
    ps = (
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        f"$s.TargetPath='{target}';"
        f"$s.WorkingDirectory='{target.parent}';"
        f"$s.IconLocation='{target}';"
        f"$s.Description='KIS 자동매매 시스템';"
        f"$s.Save()"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=30)
        if r.returncode == 0 and lnk.exists():
            print(f"바탕화면 바로가기 생성: {lnk}")
        else:
            print("바로가기 생성 실패 (exe는 정상). 직접 만들어 쓰세요.")
    except Exception as e:
        print(f"바로가기 생성 건너뜀: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
