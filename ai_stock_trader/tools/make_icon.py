"""앱 아이콘 생성기.

    python tools/make_icon.py

파란 라운드 사각형 + 흰 캔들 차트. exe 아이콘과 창 아이콘이 같은 파일을 쓴다.
윈도우가 상황에 따라 16~256px 중 하나를 고르므로 전 크기를 한 파일에 넣는다.
작은 크기에서 뭉개지지 않도록 큼직하게 그린 뒤 줄인다.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "app" / "icon.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]

BLUE = (37, 122, 224, 255)        # 본체
BLUE_DARK = (24, 92, 178, 255)    # 아래쪽 그라데이션
WHITE = (255, 255, 255, 255)


def render(px: int) -> Image.Image:
    """px 크기 아이콘 한 장. 4배로 그린 뒤 줄여서 계단현상을 없앤다."""
    s = px * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 라운드 사각형 본체 (세로 그라데이션)
    pad = int(s * 0.06)
    radius = int(s * 0.22)
    body = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    bd.rounded_rectangle([pad, pad, s - pad, s - pad], radius=radius, fill=BLUE)
    grad = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / max(s - 1, 1)
        gd.line([(0, y), (s, y)], fill=(
            int(BLUE[0] + (BLUE_DARK[0] - BLUE[0]) * t),
            int(BLUE[1] + (BLUE_DARK[1] - BLUE[1]) * t),
            int(BLUE[2] + (BLUE_DARK[2] - BLUE[2]) * t), 255))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [pad, pad, s - pad, s - pad], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)

    # 모든 크기에 똑같은 그림을 쓴다.
    # 크기별로 다른 도안을 넣으면 윈도우가 상황마다 다른 걸 골라서
    # 작업표시줄과 창 제목줄 아이콘이 서로 달라 보인다. (원래 불만이 이거였다)
    # 그래서 16px 에서도 뭉개지지 않는 굵은 상승선 하나로 통일한다.
    w = max(int(s * 0.10), 3)
    pts = [(0.26, 0.66), (0.44, 0.48), (0.56, 0.58), (0.76, 0.32)]
    d.line([(int(s * x), int(s * y)) for x, y in pts],
           fill=WHITE, width=w, joint="curve")
    # 끝점 화살촉
    hx, hy = int(s * 0.76), int(s * 0.32)
    a = int(s * 0.15)
    d.polygon([(hx + int(a * 0.30), hy - int(a * 0.30)),
               (hx - int(a * 0.62), hy - int(a * 0.16)),
               (hx - int(a * 0.16), hy + int(a * 0.62))], fill=WHITE)
    # 선 끝이 각지지 않게
    for x, y in pts:
        r = w // 2
        d.ellipse([int(s * x) - r, int(s * y) - r,
                   int(s * x) + r, int(s * y) + r], fill=WHITE)

    return img.resize((px, px), Image.LANCZOS)


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Pillow ICO 는 기준 이미지보다 큰 sizes 를 버린다.
    # 가장 큰 것을 기준으로 두고 나머지를 append 해야 전 크기가 들어간다.
    frames = sorted((render(p) for p in SIZES), key=lambda im: im.width, reverse=True)
    frames[0].save(OUT, format="ICO",
                   sizes=[(p, p) for p in SIZES],
                   append_images=frames[1:])
    got = sorted(Image.open(OUT).info.get("sizes", []))
    if len(got) != len(SIZES):
        raise SystemExit(f"크기가 빠졌습니다: 기대 {SIZES} / 실제 {got}")
    print(f"생성: {OUT}  ({OUT.stat().st_size:,} bytes)")
    print(f"  포함 크기: {[w for w, _h in got]}")

    # Tk 의 iconbitmap 은 작은 크기 하나만 읽어서 Alt-Tab 같은 큰 자리에서
    # 흐릿하게 확대된다. iconphoto 용 PNG 를 따로 실어 선명하게 만든다.
    png = OUT.parent / "icon_256.png"
    render(256).save(png)
    print(f"창 아이콘용 PNG: {png}  ({png.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
