"""텔레그램 알림.

자동매매는 자리를 비운 사이에 돈다. 체결이나 킬스위치 발동을 로그 파일에서만
확인할 수 있으면 알림의 의미가 없다.

봇 만들기:
  1. 텔레그램에서 @BotFather 에게 /newbot -> 토큰을 받는다
  2. 만든 봇에게 아무 메시지나 한 번 보낸다
  3. https://api.telegram.org/bot<토큰>/getUpdates 를 열어 chat.id 를 확인
  4. .env 에 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 를 넣는다
     ([설정] 탭에서 입력하고 [테스트 전송]을 눌러도 된다)
"""
from __future__ import annotations

import logging
import queue
import threading
import time

import requests

from .settings import _env

log = logging.getLogger(__name__)

# 어떤 이벤트를 보낼지. 시세/데이터 잡음은 보내지 않는다.
DEFAULT_KINDS = ("trade", "risk", "error", "ai")


class Notifier:
    def __init__(self, kinds: tuple = DEFAULT_KINDS, min_interval: float = 0.6):
        self.kinds = set(kinds)
        self.min_interval = min_interval
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_error = ""

    # -- 설정 ---------------------------------------------------------------
    @property
    def token(self) -> str:
        return _env("TELEGRAM_BOT_TOKEN")

    @property
    def chat_id(self) -> str:
        return _env("TELEGRAM_CHAT_ID")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    # -- 전송 ---------------------------------------------------------------
    def send(self, text: str) -> tuple[bool, str]:
        """즉시 동기 전송 (테스트 버튼용)."""
        if not self.enabled:
            return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없습니다."
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text[:4000],
                      "disable_web_page_preview": True},
                timeout=10)
            if r.status_code == 200 and r.json().get("ok"):
                return True, "전송 성공"
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, str(e)

    def push(self, kind: str, msg: str) -> None:
        """엔진 이벤트를 큐에 넣는다. 절대 블로킹하지 않는다."""
        if not self.enabled or kind not in self.kinds:
            return
        icon = {"trade": "체결", "risk": "리스크", "error": "오류", "ai": "AI"}.get(kind, kind)
        try:
            self._q.put_nowait(f"[{icon}] {msg}")
        except queue.Full:
            pass
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name="notifier")
        self._thread.start()

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self._q.get(timeout=30)
            except queue.Empty:
                return
            # 짧은 시간에 여러 건이면 묶어서 한 번에 보낸다 (도배 방지)
            batch = [msg]
            time.sleep(self.min_interval)
            while len(batch) < 10:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            ok, err = self.send("\n".join(batch))
            if not ok:
                self.last_error = err
                log.debug("텔레그램 전송 실패: %s", err)

    def stop(self) -> None:
        self._stop.set()
