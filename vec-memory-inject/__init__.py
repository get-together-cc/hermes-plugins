"""vec-memory-inject plugin.

每轮对话前自动执行本地向量记忆搜索 (bge-m3 embeddings + SQLite)，
把最相关的 top-N 条结果注入当轮用户消息作为上下文。

数据源: ~/.hermes/vec_memory.db (vec_memory.py 维护)
调用:   python3 ~/.hermes/vec-memory/vec_memory.py search "<query>" 5

设计约束:
- 失败静默 (不阻断对话)
- 注入上限 ~8000 字符 (低于 hook 10000 字符 spill 阈值)
- 无结果时不注入任何内容
"""

import json
import logging
import os
import subprocess
import threading
import time
from datetime import date, datetime
from logging.handlers import RotatingFileHandler

_LOG_DIR = os.path.expanduser("~/.hermes/logs")
_LOG_FILE = os.path.join(_LOG_DIR, "vec-memory.log")
logger = logging.getLogger("vec-memory")
logger.setLevel(logging.INFO)
if not logger.handlers:
    os.makedirs(_LOG_DIR, exist_ok=True)
    fh = RotatingFileHandler(_LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)

_SEARCH_SCRIPT = os.environ.get(
    "HERMES_VEC_MEMORY_SCRIPT",
    os.path.expanduser("~/.hermes/vec-memory/vec_memory.py"))
_EXTRACT_SCRIPT = os.environ.get(
    "HERMES_EXTRACT_SCRIPT",
    os.path.expanduser("~/.hermes/vec-memory/extract_turn.py"))
_MAX_RESULTS = 5
_MAX_CHARS = 8000

# ---- 对话捕获配置 ----
_CAPTURE_DIR = os.path.expanduser("~/.hermes/vec-memory/turns")
_CAP_USER = 1500      # 用户消息截断 (chars)
_CAP_ASSISTANT = 3000  # 助手回复截断 (chars)


def _search(query: str) -> list:
    """Run vec_memory.py search, return list of (path, score, snippet).

    取 top-10 候选，自适应过滤：动态阈值 = max(0.45, 最高分×0.75)。
    """
    try:
        proc = subprocess.run(
            ["python3", _SEARCH_SCRIPT, "search", query, "10"],
            capture_output=True, text=True, timeout=8,
        )
        if proc.returncode != 0:
            logger.warning("vec-memory search failed: %s", proc.stderr[:200])
            return []
        return _parse_output(proc.stdout)
    except Exception as exc:  # noqa: BLE001 - fail silently
        logger.warning("vec-memory search exception: %s", exc)
        return []


def _parse_output(stdout: str) -> list:
    """Parse vec_memory.py search output into structured hits.

    Actual output format:
        📝 0.685 | 维护库条目摘要 (score | 内容一行)
        📁 0.581 | 描述
             → /path/to/file.md
    """
    hits = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or "📝" not in line and "📁" not in line:
            continue
        parts = line.split("|", 1)
        if len(parts) == 2:
            score = parts[0].replace("📝", "").replace("📁", "").strip()
            content = parts[1].strip()
        else:
            score = "?"
            content = line
        try:
            score_f = float(score)
        except ValueError:
            score_f = 0.0
        hits.append({"score": score_f, "text": content})
    return hits


def _format_context(query: str, hits: list) -> str:
    """Build the injected context block with adaptive filtering.

    动态阈值: max(0.45, 最高分×0.75) — 低于阈值丢弃。
    按分数截断: >0.7 全文, 0.5-0.7 截 100 字, 其余截 50 字。
    """
    if not hits:
        return ""

    # 1. 自适应阈值过滤
    top_score = max(h["score"] for h in hits)
    threshold = max(0.45, top_score * 0.75)
    filtered = [h for h in hits if h["score"] >= threshold][:5]

    if not filtered:
        return ""

    # 2. 按分数截断长度
    def _truncate(text: str, score: float) -> str:
        if score >= 0.7:
            limit = 200
        elif score >= 0.55:
            limit = 100
        else:
            limit = 50
        return text if len(text) <= limit else text[:limit] + "…"

    lines = [
        "【向量记忆自动检索】相关维护库/技能/历史记录:",
        f"检索词: {query}",
        "",
    ]
    for i, h in enumerate(filtered, 1):
        lines.append(f"{i}. [{h['score']:.3f}] {_truncate(h['text'], h['score'])}")
    lines.append("")
    lines.append("(如需完整内容可用 read_file 读取对应文件)")
    return "\n".join(lines)


def recall_context(session_id, user_message, is_first_turn, conversation_history=None, **kwargs):
    """pre_llm_call hook: search vec memory and inject context.

    ALSO extracts the PREVIOUS turn from conversation_history (every turn
    fires this hook, so pure-text turns get extracted too — post_tool_call
    only fires when tools run, which misses chat-only turns).
    """
    # --- 1. 萃取上一轮 (conversation_history 最后一条 assistant 消息) ---
    if conversation_history and os.path.exists(_EXTRACT_SCRIPT):
        try:
            prev_assistant = ""
            prev_user = ""
            for msg in reversed(conversation_history):
                role = msg.get("role") if isinstance(msg, dict) else None
                if role == "assistant" and not prev_assistant:
                    prev_assistant = msg.get("content", "") or ""
                    break
            for msg in reversed(conversation_history):
                role = msg.get("role") if isinstance(msg, dict) else None
                if role == "user" and not prev_user:
                    prev_user = msg.get("content", "") or ""
                    break
            if prev_assistant and prev_user:
                payload = json.dumps({
                    "user": prev_user[:_CAP_USER],
                    "assistant": prev_assistant[:_CAP_ASSISTANT],
                }, ensure_ascii=False)
                threading.Thread(
                    target=_run_extract, args=(payload,), daemon=True
                ).start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("pre_llm_call extract failed: %s", exc)

    # --- 2. 向量记忆注入 ---
    query = (user_message or "").strip()
    if not query:
        return None

    hits = _search(query)
    if not hits:
        # 即使无向量记忆命中, 也注入当前时间 (2026-09-09 用户要求)
        now = datetime.now()
        return {"context": f"[当前时间: {now.strftime('%Y-%m-%d %A %H:%M (CST)')}]\n"}

    context = _format_context(query, hits)
    if len(context) > _MAX_CHARS:
        context = context[:_MAX_CHARS] + "\n...(截断)"

    # 注入当前时间(2026-09-08 用户要求: 这能让模型有时间感知, 问候/时效判断不再靠猜)
    now = datetime.now()
    time_tag = f"[当前时间: {now.strftime('%Y-%m-%d %A %H:%M (CST)')}]\n"
    context = time_tag + context

    logger.info("injected %d hits (%d chars) for query: %s",
                len(hits), len(context), query[:50])
    return {"context": context}


def capture_turn(session_id, user_message, assistant_response, **kwargs):
    """post_llm_call hook: capture turn AND extract knowledge immediately.

    NOTE: Hermes 当前版本 (turn_context.py) 只触发 pre_llm_call，
    post_llm_call 文档存在但无触发点。本函数保留以便未来版本生效；
    当前实际触发走 _on_post_tool_call (post_tool_call hook)。
    """
    try:
        os.makedirs(_CAPTURE_DIR, exist_ok=True)
        day = date.today().isoformat()
        path = os.path.join(_CAPTURE_DIR, f"{day}.jsonl")

        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "session_id": str(session_id or ""),
            "user": (user_message or "")[:_CAP_USER],
            "assistant": (assistant_response or "")[:_CAP_ASSISTANT],
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # Background extraction -> knowledge base + vector index (non-blocking)
        if os.path.exists(_EXTRACT_SCRIPT):
            payload = json.dumps({
                "user": (user_message or "")[:_CAP_USER],
                "assistant": (assistant_response or "")[:_CAP_ASSISTANT],
            }, ensure_ascii=False)
            threading.Thread(
                target=_run_extract, args=(payload,), daemon=True
            ).start()
    except Exception as exc:  # noqa: BLE001 - never break the agent
        logger.warning("vec-memory capture failed: %s", exc)
    return None  # observer hook — nothing to inject


# ---- post_tool_call 替代方案 (当前版本生效路径) ----
# 状态: (session_id, 最近萃取时间戳) -> 每轮最多萃取一次
_last_extract: dict = {}
_EXTRACT_COOLDOWN_S = 90  # 同一会话 90 秒内只萃取一次


def _on_post_tool_call(tool_name, args, result, task_id, **kwargs):
    """post_tool_call hook: 每轮对话结束后由工具调用触发萃取。

    策略: 用 task_id(会话) + 冷却时间去重，同一轮对话只萃取一次。
    萃取内容取最近一次捕获的 user/assistant 消息。
    """
    if not os.path.exists(_EXTRACT_SCRIPT):
        return None

    now = time.monotonic()
    last = _last_extract.get(task_id, 0)
    if now - last < _EXTRACT_COOLDOWN_S:
        return None
    _last_extract[task_id] = now

    try:
        # 读取最近的捕获条目 (同会话最新一条)
        day = date.today().isoformat()
        path = os.path.join(_CAPTURE_DIR, f"{day}.jsonl")
        if not os.path.exists(path):
            return None

        latest = None
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("session_id") == str(task_id or ""):
                    latest = entry
        if not latest:
            return None

        payload = json.dumps({
            "user": latest.get("user", ""),
            "assistant": latest.get("assistant", ""),
        }, ensure_ascii=False)
        threading.Thread(
            target=_run_extract, args=(payload,), daemon=True
        ).start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("post_tool_call extract failed: %s", exc)
    return None


def _run_extract(payload: str) -> None:
    """Run extract_turn.py in a subprocess (up to 60s, fully detached)."""
    try:
        proc = subprocess.run(
            ["python3", _EXTRACT_SCRIPT],
            input=payload, capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            logger.info("extract: %s", proc.stdout.strip()[:120])
    except Exception as exc:  # noqa: BLE001
        logger.warning("extract_turn failed: %s", exc)


def register(ctx):
    """Register hooks: pre_llm_call (inject) + post_tool_call (capture/extract).

    注意: 当前 Hermes 版本 post_llm_call 无触发点 (turn_context.py 只触发
    pre_llm_call)，所以捕获/萃取挂在 post_tool_call 上 (90s 冷却去重)。
    post_llm_call 注册保留，未来版本生效后自动接管。
    """
    ctx.register_hook("pre_llm_call", recall_context)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("post_llm_call", capture_turn)
    logger.info("vec-memory-inject registered (inject + capture/extract)")
