"""릴리즈 도우미.

    python release.py            현재 버전 확인
    python release.py 1.0.1      버전 올리고 커밋 + 태그 + 푸시 (GitHub Actions가 빌드)
    python release.py 1.0.1 --local   내 PC에서 빌드해서 직접 릴리즈 올리기 (gh 필요)

태그를 밀면 .github/workflows/release.yml 이 windows-latest 에서 exe를 빌드해
릴리즈에 붙인다. 소스는 저장소에, 실행 파일은 릴리즈에만 올라간다.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
VERSION_FILE = ROOT / "app" / "version.py"
# build.py 의 NAME 과 같아야 한다 (릴리즈 자산명은 ASCII - GitHub이 한글을 잘라낸다)
EXE = ROOT / "Clistock.exe"


def run(cmd: list[str], cwd: Path = REPO_ROOT, check: bool = True) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise SystemExit(f"실패: {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return (r.stdout or "").strip()


def current() -> str:
    m = re.search(r'__version__ = "([^"]+)"', VERSION_FILE.read_text(encoding="utf-8"))
    return m.group(1) if m else "0.0.0"


def bump(v: str) -> None:
    txt = VERSION_FILE.read_text(encoding="utf-8")
    txt = re.sub(r'__version__ = "[^"]+"', f'__version__ = "{v}"', txt)
    VERSION_FILE.write_text(txt, encoding="utf-8")
    print(f"app/version.py -> {v}")


def ensure_clean() -> None:
    if run(["git", "status", "--porcelain"]):
        print("커밋되지 않은 변경이 있습니다:")
        print(run(["git", "status", "--short"]))
        if input("그대로 함께 커밋할까요? [y/N] ").strip().lower() != "y":
            raise SystemExit("중단했습니다.")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    local = "--local" in sys.argv

    if not (REPO_ROOT / ".git").exists():
        print("git 저장소가 아닙니다. 먼저 아래를 실행하세요:")
        print(f"  cd {REPO_ROOT}")
        print("  git init && git add -A && git commit -m \"first commit\"")
        print("  git remote add origin https://github.com/<owner>/<repo>.git")
        return 1

    if not args:
        print(f"현재 버전  v{current()}")
        print(f"최근 태그  {run(['git', 'tag', '--sort=-v:refname'], check=False).splitlines()[:5]}")
        print()
        print(__doc__)
        return 0

    new = args[0].lstrip("vV")
    if not re.fullmatch(r"\d+\.\d+\.\d+", new):
        print(f"버전 형식이 잘못됐습니다: {new} (예: 1.0.1)")
        return 1

    tag = f"v{new}"
    if tag in run(["git", "tag"], check=False).split():
        print(f"태그 {tag} 가 이미 있습니다.")
        return 1

    ensure_clean()
    bump(new)
    run(["git", "add", "-A"])
    run(["git", "commit", "-m", f"release {tag}"])
    run(["git", "tag", "-a", tag, "-m", f"KIS 자동매매 {tag}"])

    if local:
        print("로컬 빌드 중…")
        subprocess.run([sys.executable, str(ROOT / "build.py")], cwd=ROOT, check=True)
        if not EXE.exists():
            print("exe를 찾을 수 없습니다.")
            return 1
        run(["git", "push"])
        run(["git", "push", "origin", tag])
        print("릴리즈 생성 중 (gh)…")
        run(["gh", "release", "create", tag, str(EXE),
             "--title", f"KIS 자동매매 {tag}", "--generate-notes"])
        print(f"완료: {tag} 릴리즈에 exe 업로드됨")
    else:
        print()
        print(f"{tag} 준비 완료. 아래를 실행하면 GitHub Actions가 빌드해서 릴리즈까지 만듭니다:")
        print()
        print("  git push && git push origin " + tag)
        print()
        print("(직접 빌드해 올리려면:  python release.py "
              f"{new} --local  — 단, 태그를 지우고 다시 실행해야 합니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
