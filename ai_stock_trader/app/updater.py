"""GitHub 릴리즈 자동 업데이트.

동작:
  1. GitHub Releases API로 최신 태그를 확인한다
  2. 실행 중인 버전보다 높으면 릴리즈에 붙은 exe를 내려받는다
  3. 실행 중인 exe는 자기 자신을 덮어쓸 수 없으므로(윈도우 파일 잠금),
     교체 스크립트를 만들어 띄우고 앱은 종료한다
  4. 스크립트가 프로세스 종료를 기다렸다가 파일을 바꾸고 다시 실행한다

안전장치:
  - 설정된 저장소(owner/repo)에서만 받는다
  - HTTPS + 리다이렉트 호스트 확인
  - 크기 검증 후 교체, 원본은 .bak 으로 남긴다
  - 기본값은 '확인 후 설치'. 완전 자동 설치는 사용자가 켜야 동작한다
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests

from .version import APP_NAME, GITHUB_REPO, __version__, is_newer
from .settings import _env, child_env

log = logging.getLogger(__name__)

API = "https://api.github.com/repos/{repo}/releases/latest"
ALLOWED_HOSTS = ("github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com", "api.github.com")


@dataclass
class Release:
    version: str = ""
    name: str = ""
    notes: str = ""
    url: str = ""            # exe 다운로드 주소
    size: int = 0
    html_url: str = ""
    published: str = ""
    error: str = ""

    @property
    def available(self) -> bool:
        return bool(self.url and self.version and not self.error)


def repo_name(cfg=None) -> str:
    """저장소는 설정 > .env > 코드 기본값 순으로 찾는다."""
    if cfg is not None:
        r = (getattr(cfg, "update", None) and cfg.update.repo) or ""
        if r:
            return r.strip()
    return (_env("GITHUB_REPO") or GITHUB_REPO).strip()


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def current_exe() -> Path:
    return Path(sys.executable).resolve()


# --------------------------------------------------------------------------
def check(cfg=None, timeout: int = 12) -> Release:
    repo = repo_name(cfg)
    if not repo:
        return Release(error="업데이트 저장소가 설정되지 않았습니다. "
                             "[설정] 탭에서 owner/repo 형식으로 넣어주세요.")
    if "/" not in repo:
        return Release(error=f"저장소 형식이 잘못됐습니다: {repo} (owner/repo)")

    try:
        r = requests.get(API.format(repo=repo), timeout=timeout,
                         headers={"Accept": "application/vnd.github+json"})
    except Exception as e:
        return Release(error=f"업데이트 확인 실패: {e}")

    if r.status_code == 404:
        return Release(error=f"릴리즈를 찾을 수 없습니다 ({repo}). "
                             f"저장소 이름이나 공개 여부를 확인하세요.")
    if r.status_code != 200:
        return Release(error=f"GitHub 응답 오류 {r.status_code}: {r.text[:120]}")

    d = r.json()
    tag = (d.get("tag_name") or "").strip()
    rel = Release(
        version=tag,
        name=(d.get("name") or tag).strip(),
        notes=(d.get("body") or "").strip(),
        html_url=d.get("html_url", ""),
        published=(d.get("published_at") or "")[:10],
    )
    for a in d.get("assets") or []:
        n = (a.get("name") or "").lower()
        if n.endswith(".exe"):
            rel.url = a.get("browser_download_url", "")
            rel.size = int(a.get("size") or 0)
            break
    if not rel.url:
        rel.error = "릴리즈에 exe 파일이 없습니다."
    return rel


def has_update(cfg=None) -> tuple[bool, Release]:
    rel = check(cfg)
    if rel.error:
        return False, rel
    return is_newer(rel.version, __version__), rel


# --------------------------------------------------------------------------
def _host_allowed(url: str) -> bool:
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def download(rel: Release, dest_dir: Path | None = None, progress=None) -> Path:
    """릴리즈 exe를 내려받아 임시 파일 경로를 돌려준다."""
    from urllib.parse import urlparse

    if not _host_allowed(rel.url):
        raise ValueError(f"허용되지 않은 다운로드 주소입니다: {urlparse(rel.url).netloc}")
    if not rel.url.lower().startswith("https://"):
        raise ValueError("HTTPS가 아닌 주소입니다.")

    dest_dir = Path(dest_dir or tempfile.gettempdir())
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{APP_NAME}-{rel.version}.download"

    with requests.get(rel.url, stream=True, timeout=60,
                      headers={"Accept": "application/octet-stream"}) as r:
        r.raise_for_status()
        # GitHub은 자산을 objects.githubusercontent.com 등으로 리다이렉트한다.
        # 최초 URL만 검사하면 중간 리다이렉트가 아무 데나 갈 수 있으므로
        # 실제로 응답을 준 최종 URL도 같은 허용 목록으로 확인한다.
        if not (_host_allowed(r.url) and str(r.url).lower().startswith("https://")):
            raise ValueError(f"리다이렉트가 허용되지 않은 주소로 향했습니다: "
                             f"{urlparse(str(r.url)).netloc}")
        total = int(r.headers.get("content-length") or rel.size or 0)
        done = 0
        with open(out, "wb") as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(min(done / total, 1.0), done, total)

    got = out.stat().st_size
    if got < 1_000_000:
        out.unlink(missing_ok=True)
        raise ValueError(f"받은 파일이 너무 작습니다 ({got:,} bytes). 다운로드 실패로 봅니다.")
    if rel.size and abs(got - rel.size) > 1024:
        out.unlink(missing_ok=True)
        raise ValueError(f"파일 크기가 다릅니다 (기대 {rel.size:,}, 받음 {got:,}).")
    return out


# --------------------------------------------------------------------------
_SCRIPT = """@echo off
chcp 65001 >nul
title {app} 업데이트
echo {app} 를 업데이트하는 중입니다. 창을 닫지 마세요.

rem PyInstaller onefile 런타임 변수를 지운다.
rem 이게 남아 있으면 새로 띄운 exe가 이전 프로세스의 임시폴더를 물고 들어가
rem "Security validation failure: parent process has different executable!" 로 죽는다.
set "_PYI_ARCHIVE_FILE="
set "_PYI_APPLICATION_HOME_DIR="
set "_PYI_PARENT_PROCESS_LEVEL="
set "_PYI_SPLASH_IPC="
set "_MEIPASS2="
set "PYINSTALLER_RESET_ENVIRONMENT=1"

rem 실행 중인 프로세스가 끝날 때까지 기다린다 (파일이 잠겨 있으면 교체 불가)
set /a tries=0
:wait
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if errorlevel 1 goto ready
set /a tries+=1
if %tries% GEQ 60 goto giveup
timeout /t 1 /nobreak >nul
goto wait

:ready
rem onefile 런처(부모)가 임시폴더를 정리할 시간을 준다
timeout /t 2 /nobreak >nul
if exist "{bak}" del /f /q "{bak}"

set /a swaps=0
:swap
move /y "{target}" "{bak}" >nul 2>&1
if not errorlevel 1 goto place
set /a swaps+=1
if %swaps% GEQ 15 goto locked
timeout /t 1 /nobreak >nul
goto swap

:place
move /y "{new}" "{target}" >nul 2>&1
if errorlevel 1 goto restore
echo 업데이트 완료. 다시 실행합니다.
start "" "{target}"
goto done

:restore
echo 교체에 실패했습니다. 원래 파일로 되돌립니다.
if exist "{bak}" move /y "{bak}" "{target}" >nul
start "" "{target}"
goto done

:locked
echo 파일이 잠겨 있어 교체하지 못했습니다. 원래 파일 그대로 다시 실행합니다.
start "" "{target}"
goto done

:giveup
echo 프로그램이 종료되지 않아 업데이트를 취소합니다.
pause

:done
del /f /q "%~f0" >nul 2>&1
"""


def stage_install(new_exe: Path, target: Path | None = None) -> Path:
    """교체 스크립트를 만들어 실행한다. 호출 후 앱은 바로 종료해야 한다."""
    target = Path(target or current_exe())
    script = Path(tempfile.gettempdir()) / f"{APP_NAME}_update_{os.getpid()}.bat"
    script.write_text(
        _SCRIPT.format(app=APP_NAME, pid=os.getpid(), target=str(target),
                       new=str(new_exe), bak=str(target.with_suffix(".exe.bak"))),
        encoding="utf-8")
    # 환경변수를 그대로 물려주면 새로 띄운 exe가 이 프로세스의 onefile 임시폴더를
    # 자기 것으로 착각한다. 깨끗한 환경으로 넘겨야 재실행이 성공한다.
    subprocess.Popen(["cmd", "/c", "start", "", "/min", str(script)],
                     shell=False, env=child_env(),
                     creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    log.info("업데이트 스크립트 실행: %s", script)
    return script
