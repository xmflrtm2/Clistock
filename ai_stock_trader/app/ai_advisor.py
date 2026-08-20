"""Gemini 연동.

기존 구조에서 바꾼 핵심: AI에게 "지금 살까요?"를 묻지 않는다.

  이유 - 같은 질문에 매번 다른 답이 나오면 백테스트가 불가능하고,
        백테스트가 불가능하면 그 전략이 돈을 버는지 알 방법이 없다.
        게시글이 말한 "매매기준을 애매하지 않게" 와 정면으로 충돌한다.

그래서 AI가 맡는 일은 셋:
  1. 일일 리뷰   - 쌓인 거래 기록을 읽고 무엇이 잘못됐는지 짚어준다 (사람이 읽는 용도)
  2. 파라미터 제안 - 백테스트 결과를 보고 다음 탐색 방향을 제안한다 (사람이 승인 후 반영)
  3. 진입 거부권  - 뉴스/공시 악재가 있으면 진입을 '막을 수만' 있다. 만들지는 못한다.

즉 AI는 조언자이고, 방아쇠는 백테스트된 규칙이 당긴다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from .settings import AIConfig, gemini_key
from .storage import Store

log = logging.getLogger(__name__)


class AIAdvisor:
    def __init__(self, cfg: AIConfig, store: Store):
        self.cfg = cfg
        self.store = store
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.cfg.enabled and gemini_key())

    def _c(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=gemini_key())
        return self._client

    def _ask(self, prompt: str, schema_hint: str = "") -> dict | None:
        if not self.available:
            return None
        try:
            from google.genai import types
            r = self._c().models.generate_content(
                model=self.cfg.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.4,
                ),
            )
            return json.loads(r.text)
        except Exception as e:
            log.warning("Gemini 호출 실패: %s", e)
            return None

    # ------------------------------------------------------------------
    def daily_review(self, mode: str) -> dict | None:
        """오늘 거래/신호를 요약하고 개선점을 받아 DB에 남긴다."""
        today = datetime.now().strftime("%Y-%m-%d")
        trades = [t for t in self.store.closed_trades(mode, 200)
                  if (t.get("exit_ts") or "").startswith(today)]
        signals = [s for s in self.store.recent_signals(200, mode)
                   if (s.get("ts") or "").startswith(today)]
        blocked = [s for s in signals if not s.get("acted")]

        if not trades and not signals:
            return None

        payload = {
            "date": today,
            "mode": mode,
            "closed_trades": [
                {"symbol": t["symbol"], "strategy": t["strategy"],
                 "entry": t["entry_price"], "exit": t["exit_price"],
                 "pnl": round(t["pnl"] or 0), "pnl_pct": round(t["pnl_pct"] or 0, 2),
                 "reason": t["exit_reason"]}
                for t in trades
            ],
            "signal_count": len(signals),
            "blocked_signals": [
                {"symbol": s["symbol"], "strategy": s["strategy"],
                 "blocked_by": s.get("blocked_by") or ""}
                for s in blocked[:20]
            ],
            "realized_pnl": round(self.store.realized_pnl_today(mode)),
            "consecutive_losses": self.store.consecutive_losses(mode),
        }

        prompt = f"""당신은 퀀트 트레이딩 시스템의 운용 리뷰어입니다.
아래는 오늘 하루 자동매매 시스템의 실제 기록입니다. 종목 추천이나 투자 조언은 하지 마세요.
오직 "시스템이 설계대로 동작했는가", "규칙에 어떤 결함이 보이는가"만 평가하세요.

{json.dumps(payload, ensure_ascii=False, indent=2)}

아래 JSON 형식으로만 답하세요.
{{
  "summary": "오늘 시스템 동작 요약 2~3문장 (한국어)",
  "what_worked": ["잘 작동한 점"],
  "what_failed": ["문제가 된 점 - 손절이 너무 타이트했다 / 신호가 과도하게 차단됐다 등"],
  "rule_issues": ["규칙 자체의 결함으로 의심되는 것"],
  "suggested_experiments": [
    {{"target": "risk.max_loss_per_trade_pct 같은 설정 경로",
      "change": "현재값 -> 제안값", "why": "근거"}}
  ],
  "data_gaps": ["판단에 부족했던 데이터"]
}}"""
        out = self._ask(prompt)
        if out:
            self.store.add_ai_note("daily_review", json.dumps(out, ensure_ascii=False))
        return out

    # ------------------------------------------------------------------
    def suggest_params(self, strategy: str, sweep_results: list[dict],
                       current: dict) -> dict | None:
        """그리드 탐색 결과를 보고 다음 탐색 방향을 제안."""
        top = sweep_results[:8]
        prompt = f"""당신은 백테스트 결과를 해석하는 퀀트 리서처입니다.
전략: {strategy}
현재 파라미터: {json.dumps(current, ensure_ascii=False)}

그리드 탐색 결과 상위 {len(top)}개 (return_over_mdd 내림차순):
{json.dumps(top, ensure_ascii=False, indent=2)}

과최적화를 경계하세요. 거래 수가 적거나 특정 조합만 튀는 결과는 신뢰하지 마세요.
파라미터 표면이 평탄한(주변값도 함께 좋은) 구간을 선호하세요.

아래 JSON 형식으로만 답하세요.
{{
  "recommended": {{"파라미터명": 값}},
  "confidence": "high|medium|low",
  "reasoning": "왜 이 조합인지 2~3문장 (한국어)",
  "overfit_risk": "과최적화 위험 평가 (한국어)",
  "next_grid": {{"파라미터명": [다음에 탐색해볼 값들]}}
}}"""
        out = self._ask(prompt)
        if out:
            self.store.add_ai_note("param_suggestion",
                                   json.dumps({"strategy": strategy, **out},
                                              ensure_ascii=False))
        return out

    # ------------------------------------------------------------------
    def veto(self, symbol: str, quote: dict, signal_reason: str) -> tuple[bool, str]:
        """진입 거부권. 거부 사유가 명확할 때만 True."""
        prompt = f"""자동매매 시스템이 아래 종목에 매수 진입하려 합니다.
당신의 역할은 "명백한 위험 신호가 있을 때만 진입을 막는 것"입니다.
추천을 하는 게 아니라, 막을 이유가 있는지만 판단하세요. 애매하면 반드시 통과시키세요.

종목코드: {symbol}
현재가: {quote.get('price')}
등락률: {quote.get('change_pct')}%
거래량: {quote.get('volume')}
상한가/하한가: {quote.get('upper_limit')} / {quote.get('lower_limit')}
시장경보코드: {quote.get('market_warn')}
거래정지: {quote.get('halt')}
시스템 진입근거: {signal_reason}

막아야 할 경우의 예: 거래정지/관리종목/투자경고, 상한가 근접 추격, 등락률이 비정상적으로 극단적.
아래 JSON 형식으로만 답하세요.
{{"veto": true|false, "reason": "한 문장 (한국어)"}}"""
        out = self._ask(prompt)
        if not out:
            return False, ""
        return bool(out.get("veto")), str(out.get("reason", ""))

    # ------------------------------------------------------------------
    def explain(self, question: str, context: dict | None = None) -> str:
        prompt = f"""당신은 이 자동매매 시스템의 기술 도우미입니다.
투자 추천은 하지 말고, 시스템/전략/리스크 설정에 대한 설명만 한국어로 하세요.

질문: {question}
컨텍스트: {json.dumps(context or {}, ensure_ascii=False, default=str)[:4000]}

아래 JSON 형식으로만 답하세요.
{{"answer": "설명 (한국어, 마크다운 없이 평문)"}}"""
        out = self._ask(prompt)
        return (out or {}).get("answer", "AI 응답을 받지 못했습니다.")
