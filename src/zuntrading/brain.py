"""Claude brain — triage (model rẻ) và decision (model mạnh) qua `claude -p` headless.

Nguyên tắc FAIL-CLOSED: bất kỳ lỗi nào (CLI thiếu, timeout, JSON hỏng, schema sai,
giá ảo giác) → trả None/False, log lý do. Thà lỡ cơ hội còn hơn trade trên output rác.

Đường chạy mặc định dùng login Claude Code sẵn có (không cần API key).
Nếu ANTHROPIC_API_KEY được set và package `anthropic` đã cài → dùng API trực tiếp.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import asdict, dataclass

from .config import Settings
from .prefilter import Candidate

log = logging.getLogger(__name__)

MAX_ENTRY_DRIFT_ATR = 3.0  # entry lệch quá 3×ATR so với giá hiện tại = ảo giác → bỏ


@dataclass(frozen=True)
class Signal:
    action: str  # "trade" | "skip"
    direction: str  # "long" | "short"
    entry: float
    sl: float
    tp: float
    confidence: float
    reason: str


TRIAGE_PROMPT = """Bạn là bộ lọc RÁC setup giao dịch — tầng rẻ đứng trước model đắt hơn.
Vai trò của bạn: CHỈ loại những candidate RÕ RÀNG xấu. Bạn KHÔNG phải người quyết định cuối.

Candidate (đã qua pre-filter kỹ thuật, số liệu THẬT):
{candidate}

Quy tắc:
- worth_analysis=false CHỈ KHI có lý do cụ thể rõ ràng (ngược trend mạnh, chỉ số mâu thuẫn
  nặng, giá đang vùng nhiễu không cấu trúc).
- Lưng chừng / nghi ngờ → worth_analysis=true — để model quyết định cuối phân tích sâu.
  Sau bạn còn 2 tầng bảo vệ nữa (model đắt + risk gate code).

Trả lời DUY NHẤT một JSON object, không markdown:
{{"worth_analysis": true/false, "note": "<= 15 từ lý do"}}"""

DECISION_PROMPT = """Bạn là trader kỷ luật, quản trị rủi ro là ưu tiên số 1. KHÔNG bao giờ bịa số liệu.

Candidate (từ pre-filter kỹ thuật, giá và chỉ số là THẬT):
{candidate}

Track record THẬT của chính bạn với setup này trên symbol này (lệnh gần nhất): {track}
Track record xấu → đòi hỏi setup phải RÕ RỆT hơn bình thường mới trade. Chưa có dữ liệu → trung lập.

Nhiệm vụ: quyết định trade hay bỏ. Nếu trade, đặt entry/SL/TP dựa trên cấu trúc
(swing, EMA, ATR={atr}). SL phải ở mức cấu trúc hợp lệ, không đặt bừa.
Skip là quyết định tốt khi setup không đủ rõ — đa số candidate NÊN bị skip.

Trả lời DUY NHẤT một JSON object, không markdown:
{{"action": "trade"|"skip", "direction": "{direction}", "entry": <số>, "sl": <số>,
  "tp": <số>, "confidence": <0.0-1.0>, "reason": "<= 200 ký tự"}}

Nếu action="skip": vẫn điền đủ field (entry/sl/tp có thể là 0), confidence là độ chắc của việc skip."""


def _claude_bin() -> str | None:
    """Tìm claude CLI, ƯU TIÊN native .exe.

    Trên Windows, npm shim `claude.cmd` là batch file — batch nuốt newline trong
    argument → prompt nhiều dòng bị cắt còn dòng đầu, flags phía sau bay màu.
    Native claude.exe (CreateProcess) truyền args nguyên vẹn.
    """
    exe = shutil.which("claude.exe")
    if exe:
        return exe
    p = shutil.which("claude")
    if p and p.lower().endswith((".cmd", ".bat")):
        log.warning("chỉ tìm thấy %s (batch shim) — prompt nhiều dòng có thể bị cắt", p)
    return p


def _parse_envelope(stdout: str | None) -> dict | None:
    """Envelope CLI là 1 dòng JSON, nhưng hook của user có thể in rác lẫn vào stdout.

    Thử parse cả cục trước; fail thì quét từng dòng từ DƯỚI lên, lấy dòng JSON
    đầu tiên có key 'result' (envelope luôn in cuối).
    """
    if not stdout:
        return None
    try:
        obj = json.loads(stdout)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "result" in obj:
            return obj
    return None


def _run_claude(prompt: str, model: str, timeout: int) -> str | None:
    """Gọi claude CLI headless, trả về text kết quả hoặc None."""
    exe = _claude_bin()
    if exe is None:
        log.error("claude CLI không có trên PATH")
        return None
    try:
        proc = subprocess.run(
            # --setting-sources "": cô lập khỏi settings/hooks của user trên máy —
            # hook Stop/SessionEnd từng nhiễm cả exit code lẫn RESULT của não bot.
            [exe, "-p", prompt, "--model", model, "--output-format", "json",
             "--setting-sources", ""],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        log.warning("claude -p timeout sau %ss (model=%s)", timeout, model)
        return None
    # Parse envelope TRƯỚC khi nhìn exit code: hook SessionEnd của user fail
    # có thể đầu độc exit code dù LLM đã trả lời thành công.
    envelope = _parse_envelope(proc.stdout)
    if envelope is None:
        log.warning(
            "claude -p không trả envelope JSON (exit %s): %.300s",
            proc.returncode, proc.stderr or proc.stdout,
        )
        return None
    if envelope.get("is_error"):
        status = envelope.get("api_error_status")
        if status == 401:
            log.error(
                "claude CLI CHƯA ĐĂNG NHẬP (API 401). Chạy `claude` trong terminal"
                " rồi gõ /login một lần — bot sẽ tự dùng được từ cycle sau."
            )
        else:
            log.warning("claude -p báo lỗi: %.300s", envelope.get("result"))
        return None
    if proc.returncode != 0:
        log.warning(
            "claude -p exit %s nhưng envelope hợp lệ (hook lỗi?) → vẫn dùng kết quả",
            proc.returncode,
        )
    return envelope.get("result")


def _run_api(prompt: str, model: str, timeout: int, api_key: str) -> str | None:
    """Fallback: gọi Anthropic API trực tiếp (cần `pip install anthropic`)."""
    try:
        import anthropic
    except ImportError:
        log.warning("ANTHROPIC_API_KEY set nhưng package anthropic chưa cài → dùng CLI")
        return None
    alias = {"haiku": "claude-haiku-4-5-20251001", "sonnet": "claude-sonnet-4-6"}
    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        msg = client.messages.create(
            model=alias.get(model, model),
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        texts = [t for b in msg.content if (t := getattr(b, "text", None))]
        return texts[0] if texts else None
    except Exception as e:  # noqa: BLE001 — mọi lỗi API đều fail-closed
        log.warning("anthropic API lỗi: %s", e)
        return None


def _ask(prompt: str, model: str, settings: Settings) -> str | None:
    if settings.anthropic_api_key:
        out = _run_api(prompt, model, settings.models.timeout_seconds, settings.anthropic_api_key)
        if out is not None:
            return out
    return _run_claude(prompt, model, settings.models.timeout_seconds)


def _extract_json(text: str) -> dict | None:
    """Lấy JSON object đầu tiên trong text (chịu được ```fence``` và chữ thừa)."""
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def parse_signal(text: str | None, candidate: Candidate) -> Signal | None:
    """Validate output LLM → Signal. Mọi vi phạm schema/logic → None (kèm log)."""
    if text is None:
        return None
    obj = _extract_json(text)
    if obj is None:
        log.warning("decision không chứa JSON hợp lệ: %.200s", text)
        return None
    try:
        sig = Signal(
            action=str(obj["action"]),
            direction=str(obj["direction"]),
            entry=float(obj["entry"]),
            sl=float(obj["sl"]),
            tp=float(obj["tp"]),
            confidence=float(obj["confidence"]),
            reason=str(obj["reason"])[:200],
        )
    except (KeyError, TypeError, ValueError) as e:
        log.warning("decision thiếu/sai field: %s", e)
        return None

    if sig.action not in ("trade", "skip"):
        log.warning("action lạ: %s", sig.action)
        return None
    if sig.action == "skip":
        return sig
    if sig.direction != candidate.direction:
        log.warning("LLM đổi hướng (%s ≠ %s) → bỏ", sig.direction, candidate.direction)
        return None
    if not 0.0 <= sig.confidence <= 1.0:
        log.warning("confidence ngoài [0,1]: %s", sig.confidence)
        return None
    if sig.direction == "long" and not (sig.sl < sig.entry < sig.tp):
        log.warning("long nhưng sl/entry/tp sai thứ tự: %s/%s/%s", sig.sl, sig.entry, sig.tp)
        return None
    if sig.direction == "short" and not (sig.tp < sig.entry < sig.sl):
        log.warning("short nhưng tp/entry/sl sai thứ tự: %s/%s/%s", sig.tp, sig.entry, sig.sl)
        return None
    if candidate.atr > 0 and abs(sig.entry - candidate.price) > MAX_ENTRY_DRIFT_ATR * candidate.atr:
        log.warning(
            "entry %.4f lệch >%sxATR so với giá %.4f → ảo giác, bỏ",
            sig.entry, MAX_ENTRY_DRIFT_ATR, candidate.price,
        )
        return None
    return sig


def triage(candidate: Candidate, settings: Settings) -> bool:
    """Model rẻ trả lời: candidate có đáng phân tích sâu không. Lỗi → False."""
    prompt = TRIAGE_PROMPT.format(candidate=json.dumps(asdict(candidate), ensure_ascii=False))
    text = _ask(prompt, settings.models.triage, settings)
    obj = _extract_json(text or "")
    if obj is None:
        log.warning(
            "triage không trả JSON → bỏ candidate %s (text: %.200s)", candidate.symbol, text
        )
        return False
    worth = bool(obj.get("worth_analysis", False))
    if not worth:
        log.info(
            "triage bỏ %s %s — lý do: %s",
            candidate.symbol, candidate.setup_type, obj.get("note", "(không ghi)"),
        )
    return worth


def decide(
    candidate: Candidate, settings: Settings, track_record: dict | None = None
) -> Signal | None:
    """Model mạnh ra quyết định cuối. Trả Signal đã validate, hoặc None.

    track_record: trading memory từ journal.setup_stats — não thấy lịch sử của chính nó."""
    if track_record and track_record.get("n"):
        track = (
            f"{track_record['n']} lệnh: {track_record['wins']} thắng, "
            f"{track_record['losses']} thua, tổng P&L {track_record['pnl']:+.2f} USD"
        )
    else:
        track = "chưa có dữ liệu (chưa từng trade setup này trên symbol này)"
    prompt = DECISION_PROMPT.format(
        candidate=json.dumps(asdict(candidate), ensure_ascii=False),
        atr=candidate.atr,
        direction=candidate.direction,
        track=track,
    )
    text = _ask(prompt, settings.models.decision, settings)
    return parse_signal(text, candidate)
