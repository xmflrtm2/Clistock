"""버전 정보.

빌드할 때 이 값을 올리고 같은 이름의 git 태그(v1.0.0)를 밀면 릴리즈가 만들어진다.
실행 중인 앱은 이 값과 GitHub 최신 릴리즈를 비교해 업데이트 여부를 판단한다.
"""

__version__ = "1.1.0"

# 자동 업데이트를 받아올 GitHub 저장소 (owner/repo).
# 비워두면 업데이트 확인을 하지 않는다. [설정] 탭이나 .env(GITHUB_REPO)로도 지정 가능.
GITHUB_REPO = "xmflrtm2/Clistock"

APP_NAME = "KIS자동매매"


def parse(v: str) -> tuple:
    """'v1.2.3' / '1.2.3-beta' 를 비교 가능한 튜플로."""
    s = str(v or "").strip().lstrip("vV").split("-")[0].split("+")[0]
    out = []
    for part in s.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse(remote) > parse(local)
