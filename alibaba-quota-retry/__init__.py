"""alibaba-quota-retry — 阿里云 token-plan 429/insufficient_quota 重分类插件。

背景（2026-09-09 实测）：阿里云 model-studio 的短窗口 token 吞吐（TPM）打满时
返回 429 + insufficient_quota（"Allocated quota exceeded"），但分钟级窗口重置后
即恢复。Hermes 内置分类器把 insufficient_quota 归为 billing（确认没钱）→ 不重试
直接断流。本插件把"阿里云 + 429 + insufficient_quota"重分类为 rate_limit →
走内置退避重试链（读 Retry-After / jittered backoff / 可中断）。

只收窄不改宽：仅当 provider 是 alibaba* 且 status_code==429 且 code 是
insufficient_quota 时才接管；真·没钱（402/其他 code）仍走 billing。
"""

import logging

log = logging.getLogger(__name__)

# 匹配所有阿里云系 provider id：alibaba-token-plan-cn / alibaba-coding-plan-cn / openai 兼容等
_ALIBABA_PROVIDERS = ("alibaba", "dashscope", "tongyi")


def _reclassify(*, provider: str = "", status_code=None, error_code: str = "",
                error_message: str = "", **_kwargs):
    """429 + insufficient_quota on Alibaba → rate_limit（延迟重试），其余不动。"""
    try:
        if status_code != 429:
            return None
        code = (error_code or "").strip().lower()
        msg = (error_message or "").lower()
        if code != "insufficient_quota" and "allocated quota exceeded" not in msg:
            return None
        prov = (provider or "").strip().lower()
        if not any(p in prov for p in _ALIBABA_PROVIDERS):
            return None
        log.info("alibaba-quota-retry: reclassifying 429/insufficient_quota "
                 "(provider=%s) billing -> rate_limit for backoff retry", prov)
        return {
            "reason": "rate_limit",
            "retryable": True,
            "message": "阿里云短窗口配额触顶（分钟级自动恢复）——延迟重试中",
        }
    except Exception:
        # 永不阻断错误处理链
        return None


def register(ctx):
    ctx.register_hook("transform_api_error_classification", _reclassify)
