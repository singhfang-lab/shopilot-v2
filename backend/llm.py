"""
Multi-model LLM abstraction layer.

Supported providers: claude | openai

Public API:
  stream_chat(message, history, system_prompt, cfg) -> AsyncGenerator[str, None]
  complete(prompt, system_prompt, cfg, temperature, max_tokens) -> str
  get_active_config(db) -> LLMConfig
"""
from __future__ import annotations

import json
import os
from typing import AsyncGenerator, Optional

import httpx
from sqlmodel import Session, select

from .db import LLMConfig


# ── Defaults ──────────────────────────────────────────────────────────────────

PROVIDERS = ["claude", "openai"]

DEFAULT_MODELS = {
    "claude": "claude-sonnet-4-6",
    "openai": "gpt-4o",
}

CLAUDE_API_BASE = "https://api.anthropic.com/v1"
OPENAI_API_BASE = "https://api.openai.com/v1"


# ── Config helpers ────────────────────────────────────────────────────────────

def get_active_config(db: Session) -> LLMConfig:
    """Return the most-recently-updated active LLM config, falling back to env vars."""
    cfg = db.exec(
        select(LLMConfig).where(LLMConfig.is_active == True).order_by(LLMConfig.updated_at.desc())
    ).first()
    if cfg:
        return cfg
    return LLMConfig(
        provider="claude",
        model=DEFAULT_MODELS["claude"],
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        is_active=True,
    )


def _resolve_key(cfg: LLMConfig) -> str:
    if cfg.api_key:
        return cfg.api_key
    env_map = {
        "claude": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
    }
    return os.environ.get(env_map.get(cfg.provider, ""), "")


# ── Streaming chat ────────────────────────────────────────────────────────────

async def stream_chat(
    message: str,
    history: list[dict],
    system_prompt: str,
    cfg: LLMConfig,
) -> AsyncGenerator[str, None]:
    provider = cfg.provider
    if provider == "claude":
        async for chunk in _stream_claude(message, history, system_prompt, cfg):
            yield chunk
    elif provider == "openai":
        async for chunk in _stream_openai(message, history, system_prompt, cfg):
            yield chunk
    else:
        raise ValueError(f"Unknown provider: {provider}")


# ── Non-streaming complete ────────────────────────────────────────────────────

async def complete(
    prompt: str,
    system_prompt: str = "",
    cfg: Optional[LLMConfig] = None,
    temperature: float = 0.5,
    max_tokens: int = 1024,
) -> str:
    if cfg is None:
        cfg = LLMConfig(
            provider="claude",
            model=DEFAULT_MODELS["claude"],
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            is_active=True,
        )
    provider = cfg.provider
    if provider == "claude":
        return await _complete_claude(prompt, system_prompt, cfg, temperature, max_tokens)
    elif provider == "openai":
        return await _complete_openai(prompt, system_prompt, cfg, temperature, max_tokens)
    raise ValueError(f"Unknown provider: {provider}")


# ── Claude ────────────────────────────────────────────────────────────────────

import logging
_log = logging.getLogger(__name__)


def _build_cached_system(system_prompt: str) -> list[dict]:
    """Split system prompt into cached static layer + dynamic merchant layer."""
    split_marker = "\n## About This Merchant"
    if split_marker in system_prompt:
        static_part, dynamic_part = system_prompt.split(split_marker, 1)
        return [
            {
                "type": "text",
                "text": static_part.strip(),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": split_marker + dynamic_part,
            },
        ]
    return [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _claude_messages(message: str, history: list[dict]) -> list[dict]:
    msgs = []
    for turn in history:
        content = turn.get("content", "")
        if isinstance(content, str) and content.strip():
            msgs.append({"role": turn.get("role", "user"), "content": content})
        elif isinstance(content, list) and content:
            msgs.append({"role": turn.get("role", "user"), "content": content})
    msgs.append({"role": "user", "content": message.strip() or "请分析上传的文件"})
    return msgs


async def _stream_claude(
    message: str, history: list[dict], system_prompt: str, cfg: LLMConfig
) -> AsyncGenerator[str, None]:
    key = _resolve_key(cfg)
    model = cfg.model or DEFAULT_MODELS["claude"]
    payload = {
        "model": model,
        "max_tokens": 4096,
        "system": _build_cached_system(system_prompt),
        "messages": _claude_messages(message, history),
        "stream": True,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream(
            "POST", f"{CLAUDE_API_BASE}/messages",
            json=payload,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "prompt-caching-2024-07-31",
                "content-type": "application/json",
            },
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise RuntimeError(f"Claude {resp.status_code}: {body.decode()[:200]}")
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        if obj.get("type") == "message_delta":
                            usage = obj.get("usage", {})
                            cache_read = usage.get("cache_read_input_tokens", 0)
                            cache_create = usage.get("cache_creation_input_tokens", 0)
                            if cache_read or cache_create:
                                _log.debug("cache hit=%d create=%d", cache_read, cache_create)
                        if obj.get("type") == "content_block_delta":
                            text = obj.get("delta", {}).get("text", "")
                            if text:
                                yield text
                    except Exception:
                        pass


async def _complete_claude(
    prompt: str, system_prompt: str, cfg: LLMConfig,
    temperature: float, max_tokens: int
) -> str:
    key = _resolve_key(cfg)
    model = cfg.model or DEFAULT_MODELS["claude"]
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system_prompt,
        "messages": [{"role": "user", "content": prompt}],
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{CLAUDE_API_BASE}/messages", json=payload,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        )
        resp.raise_for_status()
        return resp.json().get("content", [{}])[0].get("text", "")


# ── OpenAI ────────────────────────────────────────────────────────────────────

def _openai_messages(message: str, history: list[dict], system_prompt: str) -> list[dict]:
    msgs = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    for turn in history:
        msgs.append({"role": turn.get("role", "user"), "content": turn.get("content", "")})
    msgs.append({"role": "user", "content": message})
    return msgs


async def _stream_openai(
    message: str, history: list[dict], system_prompt: str, cfg: LLMConfig
) -> AsyncGenerator[str, None]:
    key = _resolve_key(cfg)
    model = cfg.model or DEFAULT_MODELS["openai"]
    payload = {
        "model": model,
        "messages": _openai_messages(message, history, system_prompt),
        "stream": True,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream(
            "POST", f"{OPENAI_API_BASE}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise RuntimeError(f"OpenAI {resp.status_code}: {body.decode()[:200]}")
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        text = obj.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if text:
                            yield text
                    except Exception:
                        pass


async def _complete_openai(
    prompt: str, system_prompt: str, cfg: LLMConfig,
    temperature: float, max_tokens: int
) -> str:
    key = _resolve_key(cfg)
    model = cfg.model or DEFAULT_MODELS["openai"]
    msgs = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    msgs.append({"role": "user", "content": prompt})
    payload = {"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{OPENAI_API_BASE}/chat/completions", json=payload,
            headers={"Authorization": f"Bearer {key}"},
        )
        resp.raise_for_status()
        return resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")


# ── SQL Agent loop (Claude tool_use) ─────────────────────────────────────────

from typing import Callable, AsyncGenerator as _AG


def _summarize_result(result_text: str, max_rows: int = 20) -> str:
    """Truncate large SQL results to avoid polluting context with raw data."""
    lines = result_text.strip().split("\n")
    if len(lines) <= max_rows + 2:
        return result_text
    header = lines[:2]
    sample = lines[2:max_rows + 2]
    return "\n".join(header + sample) + f"\n... (共 {len(lines)-2} 行，已截断显示前 {max_rows} 行)"


async def run_sql_agent_loop(
    message: str,
    history: list[dict],
    system_prompt: str,
    cfg: LLMConfig,
    execute_sql_fn: Callable[[str], "Awaitable[str]"],
    max_rounds: int = 6,
) -> _AG[dict, None]:
    """
    Claude tool_use loop for SQL data analysis.

    Yields dicts:
      {"type": "tool_call",   "reason": str}   — LLM decided to query
      {"type": "tool_result", "summary": str}  — query result (truncated)
      {"type": "text_delta",  "text": str}     — final answer chunk

    Supports Claude and OpenAI (+ OpenAI-compatible endpoints like Ollama).
    Falls back to plain stream_chat for unsupported providers.
    """
    if cfg.provider == "openai":
        async for item in _sql_agent_openai(message, history, system_prompt, cfg, execute_sql_fn, max_rounds):
            yield item
        return

    if cfg.provider != "claude":
        async for chunk in stream_chat(message, history, system_prompt, cfg):
            yield {"type": "text_delta", "text": chunk}
        return

    from .data_agent import SQL_TOOL_DEFINITION

    key = _resolve_key(cfg)
    model = cfg.model or DEFAULT_MODELS["claude"]
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "prompt-caching-2024-07-31",
        "content-type": "application/json",
    }
    cached_system = _build_cached_system(system_prompt)
    messages = _claude_messages(message, history)

    for round_num in range(max_rounds):
        is_last = round_num == max_rounds - 1

        if is_last:
            # P1-B: steer the model toward a data-grounded conclusion with analysis_card
            messages.append({
                "role": "user",
                "content": "[系统提示] 请基于以上查询结果给出最终分析，务必引用具体数字支撑结论，使用 analysis_card 作为最后元素。",
            })
            payload = {
                "model": model,
                "max_tokens": 4096,
                "system": cached_system,
                "messages": messages,
                "stream": True,
            }
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST", f"{CLAUDE_API_BASE}/messages",
                    json=payload, headers=headers,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        raise RuntimeError(f"Claude {resp.status_code}: {body.decode()[:200]}")
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data.strip() == "[DONE]":
                                return
                            try:
                                obj = json.loads(data)
                                if obj.get("type") == "message_delta":
                                    usage = obj.get("usage", {})
                                    cache_read = usage.get("cache_read_input_tokens", 0)
                                    cache_create = usage.get("cache_creation_input_tokens", 0)
                                    if cache_read or cache_create:
                                        _log.debug("sql_agent cache hit=%d create=%d", cache_read, cache_create)
                                if obj.get("type") == "content_block_delta":
                                    text = obj.get("delta", {}).get("text", "")
                                    if text:
                                        yield {"type": "text_delta", "text": text}
                            except Exception:
                                pass
            return

        # Non-final round: non-streaming with tools
        payload = {
            "model": model,
            "max_tokens": 4096,
            "system": cached_system,
            "messages": messages,
            "tools": [SQL_TOOL_DEFINITION],
            "tool_choice": {"type": "auto"},
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{CLAUDE_API_BASE}/messages",
                json=payload, headers=headers,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Claude {resp.status_code}: {resp.text[:200]}")
            response = resp.json()

        # Log cache stats for non-streaming rounds
        usage = response.get("usage", {})
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_create = usage.get("cache_creation_input_tokens", 0)
        if cache_read or cache_create:
            _log.debug("sql_agent round=%d cache hit=%d create=%d", round_num, cache_read, cache_create)

        stop_reason = response.get("stop_reason")
        content_blocks = response.get("content", [])
        messages.append({"role": "assistant", "content": content_blocks})

        if stop_reason == "end_turn":
            # If the model answered without querying on round 0, nudge it to query first
            if round_num == 0:
                _log.debug("sql_agent round=0 end_turn without tool_use — nudging to query")
                messages.append({
                    "role": "user",
                    "content": "[系统提示] 你有可用的数据表，请先执行 SQL 查询获取实际数据，再基于真实数字给出分析。",
                })
                continue
            for block in content_blocks:
                if block.get("type") == "text":
                    yield {"type": "text_delta", "text": block["text"]}
            return

        if stop_reason != "tool_use":
            for block in content_blocks:
                if block.get("type") == "text":
                    yield {"type": "text_delta", "text": block["text"]}
            return

        # Execute all tool_use blocks
        tool_results = []
        any_error = False
        for block in content_blocks:
            if block.get("type") != "tool_use":
                continue

            sql = block.get("input", {}).get("sql", "")
            reason = block.get("input", {}).get("reason", "查询数据中")
            yield {"type": "tool_call", "reason": reason}

            try:
                result_text = await execute_sql_fn(sql)
            except Exception as e:
                result_text = f"工具执行错误：{e}"
                any_error = True

            if result_text.startswith("工具执行错误"):
                any_error = True

            yield {"type": "tool_result", "summary": result_text[:120]}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": _summarize_result(result_text) if not result_text.startswith("工具执行错误") else "上一条 SQL 有误，请修正后重试",
            })

        # P2-A: tool_results already contain compact error summaries for failed calls;
        # just append normally — error content was already replaced above
        if any_error:
            _log.debug("sql_agent round=%d: tool error(s), compact error summary in messages", round_num)
        messages.append({"role": "user", "content": tool_results})


# ── OpenAI SQL Agent ──────────────────────────────────────────────────────────

def _claude_tool_to_openai(tool: dict) -> dict:
    """Convert Claude input_schema tool definition to OpenAI function format."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }


async def _sql_agent_openai(
    message: str,
    history: list[dict],
    system_prompt: str,
    cfg: LLMConfig,
    execute_sql_fn,
    max_rounds: int,
):
    from .data_agent import SQL_TOOL_DEFINITION

    key = _resolve_key(cfg)
    model = cfg.model or DEFAULT_MODELS["openai"]
    base_url = getattr(cfg, "base_url", None) or OPENAI_API_BASE
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    tools = [_claude_tool_to_openai(SQL_TOOL_DEFINITION)]
    messages = _openai_messages(message, history, system_prompt)

    for round_num in range(max_rounds):
        is_last = round_num == max_rounds - 1

        if is_last:
            payload = {"model": model, "messages": messages, "stream": True}
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST", f"{base_url}/chat/completions",
                    json=payload, headers=headers,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        raise RuntimeError(f"OpenAI {resp.status_code}: {body.decode()[:200]}")
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data.strip() == "[DONE]":
                                return
                            try:
                                obj = json.loads(data)
                                text = obj["choices"][0]["delta"].get("content", "")
                                if text:
                                    yield {"type": "text_delta", "text": text}
                            except Exception:
                                pass
            return

        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                json=payload, headers=headers,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"OpenAI {resp.status_code}: {resp.text[:200]}")
            response = resp.json()

        choice = response["choices"][0]
        finish_reason = choice["finish_reason"]
        assistant_msg = choice["message"]
        messages.append(assistant_msg)

        if finish_reason == "stop":
            text = assistant_msg.get("content") or ""
            if text:
                yield {"type": "text_delta", "text": text}
            return

        if finish_reason != "tool_calls":
            text = assistant_msg.get("content") or ""
            if text:
                yield {"type": "text_delta", "text": text}
            return

        # Execute tool calls
        for tc in assistant_msg.get("tool_calls", []):
            fn = tc["function"]
            try:
                args = json.loads(fn["arguments"])
            except Exception:
                args = {}

            sql = args.get("sql", "")
            reason = args.get("reason", "查询数据中")
            yield {"type": "tool_call", "reason": reason}

            try:
                result_text = await execute_sql_fn(sql)
            except Exception as e:
                result_text = f"工具执行错误：{e}"

            yield {"type": "tool_result", "summary": result_text[:120]}
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result_text,
            })
