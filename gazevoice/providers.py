"""API providers: DeepSeek, Qwen and custom OpenAI-compatible services.

The addresses are public and built in for the presets. Only the key is private:
it's typed into the dashboard, kept in the macOS Keychain, and sent nowhere
but the provider's own Authorization header.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
from typing import Mapping

from .runner import CommandRunner

KINDS = ("deepseek", "qwen", "custom")
DEEPSEEK_BASE = "https://api.deepseek.com"
# region → (label, shared host for accounts without a Workspace ID, if Alibaba runs one there)
QWEN_REGIONS: dict[str, tuple[str, str | None]] = {
    "ap-southeast-1": ("Singapore", "dashscope-intl.aliyuncs.com"),
    "us-east-1": ("US (Virginia)", None),
    "cn-beijing": ("China (Beijing)", "dashscope.aliyuncs.com"),
    "cn-hongkong": ("Hong Kong", None),
    "eu-central-1": ("Germany (Frankfurt)", None),
    "ap-northeast-1": ("Japan (Tokyo)", None),
}
THINKING: dict[str, tuple[str, ...]] = {
    "deepseek": ("default", "off", "low", "high", "max"),
    "qwen": ("default", "off", "on"),
    "custom": ("default",),
}
KEYCHAIN_SERVICE = "GazeVoice"
_ID = re.compile(r"^[a-z0-9_]{1,32}$")
_WORKSPACE = re.compile(r"^[a-z0-9-]{1,64}$")
_KEY = re.compile(r"^[!#-\[\]-~]+$")  # printable ASCII without spaces, quotes or backslashes


class ProviderError(Exception):
    """A provider call failed; the message is plain, says what to do, and never holds the key."""


def normalize_base_url(url: str) -> str:
    """The base address, without a trailing slash or a pasted /chat/completions."""
    url = (url or "").strip()
    if not re.fullmatch(r"https?://[^\s/]+(/\S*)?", url):
        raise ValueError("Enter the provider's address, starting with https://")
    url = url.rstrip("/")
    for tail in ("/chat/completions", "/completions"):
        if url.endswith(tail):
            url = url[: -len(tail)]
    return url.rstrip("/")


def clean_key(raw: str) -> str:
    key = (raw or "").strip()
    if not key:
        raise ValueError("Paste your API key first.")
    if not _KEY.fullmatch(key):
        raise ValueError("That key has spaces or characters an API key can't have. Copy it again and paste it.")
    return key


def custom_id(name: str, taken: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:20] or "provider"
    base = f"custom_{slug}"
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}_{n}"
        n += 1
    return candidate[:32]


def thinking_fields(kind: str, thinking: str) -> dict:
    """What a thinking choice adds to the request body for this kind of provider."""
    if kind == "deepseek":
        if thinking == "off":
            return {"thinking": {"type": "disabled"}}
        if thinking in ("low", "high", "max"):
            return {"reasoning_effort": thinking}
    if kind == "qwen" and thinking in ("on", "off"):
        return {"enable_thinking": thinking == "on"}
    return {}


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    kind: str
    name: str
    base_url: str = ""  # Custom only (or a test override)
    region: str = ""  # Qwen only
    workspace: str = ""  # Qwen only, optional
    key_hint: str = ""  # the key's last four characters, once saved

    def endpoint(self) -> str:
        if self.base_url:
            return self.base_url
        if self.kind == "deepseek":
            return DEEPSEEK_BASE
        if self.kind == "qwen":
            label, shared = QWEN_REGIONS.get(self.region, (self.region, None))
            if self.workspace:
                return f"https://{self.workspace}.{self.region}.maas.aliyuncs.com/compatible-mode/v1"
            if shared:
                return f"https://{shared}/compatible-mode/v1"
            raise ValueError(f"Qwen in {label} needs your Workspace ID (Model Studio › Workspace Details).")
        raise ValueError(f"{self.name} has no address yet.")

    @property
    def region_label(self) -> str:
        return QWEN_REGIONS.get(self.region, (self.region, None))[0]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping) -> "ProviderConfig":
        if not isinstance(data, Mapping):
            raise ValueError("A provider must be an object.")
        pid, kind = str(data.get("id", "")), data.get("kind")
        if not _ID.fullmatch(pid):
            raise ValueError("A provider id is lowercase letters, digits and underscores.")
        if kind not in KINDS:
            raise ValueError(f"A provider is one of {', '.join(KINDS)}.")
        name = str(data.get("name") or "").strip()[:40]
        if not name:
            raise ValueError("A provider needs a name.")
        base_url = normalize_base_url(data["base_url"]) if data.get("base_url") else ""
        if kind == "custom" and not base_url:
            raise ValueError("A custom provider needs its address.")
        region = str(data.get("region") or ("ap-southeast-1" if kind == "qwen" else ""))
        if kind == "qwen" and region not in QWEN_REGIONS:
            raise ValueError(f"Qwen's region is one of {', '.join(QWEN_REGIONS)}.")
        workspace = str(data.get("workspace") or "").strip().lower()
        if workspace and not _WORKSPACE.fullmatch(workspace):
            raise ValueError("A Workspace ID is letters, digits and dashes.")
        key_hint = str(data.get("key_hint") or "")
        if not re.fullmatch(r"[!#-\[\]-~]{0,4}", key_hint):
            key_hint = ""
        return cls(pid, kind, name, base_url, region, workspace, key_hint)


def preset(kind: str) -> ProviderConfig:
    if kind == "deepseek":
        return ProviderConfig("deepseek", "deepseek", "DeepSeek")
    if kind == "qwen":
        return ProviderConfig("qwen", "qwen", "Qwen", region="ap-southeast-1")
    raise ValueError(f"No preset for {kind}.")


class KeyStore:
    """API keys in the macOS Keychain: service GazeVoice, one account per provider id."""

    def __init__(self, runner: CommandRunner):
        self.runner = runner

    @staticmethod
    def _account(pid: str) -> str:
        if not _ID.fullmatch(pid or ""):
            raise ValueError("Unknown provider.")
        return pid

    def _find(self, pid: str, secret: bool):
        argv = ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", self._account(pid)]
        return self.runner.run(argv + ["-w"] if secret else argv, timeout=15)

    def set(self, pid: str, key: str) -> None:
        account, key = self._account(pid), clean_key(key)
        # `security -i` reads the command from stdin, so the key never shows up in a process listing.
        command = f'add-generic-password -U -s {KEYCHAIN_SERVICE} -a {account} -w "{key}"\n'
        self.runner.run(["security", "-i"], input_text=command, timeout=15)
        if not self.has(pid):
            raise ProviderError("macOS Keychain didn't save the key. Try again, and allow access if macOS asks.")

    def get(self, pid: str) -> str | None:
        result = self._find(pid, secret=True)
        key = result.stdout.strip() if result.returncode == 0 else ""
        return key or None

    def has(self, pid: str) -> bool:
        return self._find(pid, secret=False).returncode == 0

    def delete(self, pid: str) -> None:
        self.runner.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", self._account(pid)],
                        timeout=15)


class ChatClient:
    """OpenAI-compatible chat over plain HTTPS (standard library only)."""

    def __init__(self, config: ProviderConfig, key: str, timeout: float = 120.0):
        self.config = config
        self.base = config.endpoint()
        self._key = key
        self.timeout = timeout

    def models(self) -> list[str]:
        data = self._request("GET", "/models", None, self.timeout)
        items = data.get("data") if isinstance(data, dict) else None
        return [m["id"] for m in items or [] if isinstance(m, dict) and isinstance(m.get("id"), str)]

    def chat(self, body: dict, timeout: float | None = None) -> dict:
        # Alibaba only lets Qwen think in streamed calls, so Qwen chats always stream.
        stream = self.config.kind == "qwen"
        if stream:
            body = {**body, "stream": True}
        return self._request("POST", "/chat/completions", body, timeout or self.timeout, model=body.get("model"),
                             stream=stream)

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "••••") if self._key else text

    def _request(self, method: str, path: str, body: dict | None, timeout: float, model: str | None = None,
                 stream: bool = False) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in (1, 2):
            request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    if stream:
                        return self._assemble(response, model)
                    return json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as exc:
                with exc:
                    detail = self._detail(exc)
                    if exc.code == 429 and attempt == 1:
                        time.sleep(min(_seconds(exc.headers.get("Retry-After")), 10.0))
                        continue
                    raise ProviderError(self._scrub(self._explain(exc.code, detail, model, path))) from None
            except TimeoutError:
                raise ProviderError(f"{self.config.name} took too long to answer.") from None
            except (urllib.error.URLError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                raise ProviderError(self._scrub(f"Couldn't reach {self.config.name}: {reason}")) from None
            except ValueError:
                raise ProviderError(f"{self.config.name} sent back something that isn't JSON.") from None
        raise ProviderError(f"{self.config.name} is rate-limiting requests. Wait a moment and try again.")

    def _assemble(self, response, model: str | None) -> dict:
        """A streamed (server-sent events) reply, put back together as one ordinary chat completion."""
        message: dict = {"role": "assistant", "content": ""}
        finish = None
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if isinstance(chunk.get("error"), dict):
                detail = str(chunk["error"].get("message") or "")
                raise ProviderError(self._scrub(self._explain(400, detail, model, "/chat/completions")))
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                for key in ("content", "reasoning_content"):
                    if delta.get(key):
                        message[key] = message.get(key, "") + delta[key]
                for part in delta.get("tool_calls") or []:
                    calls = message.setdefault("tool_calls", [])
                    index = part.get("index", len(calls))
                    while len(calls) <= index:
                        calls.append({"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                    slot = calls[index]
                    slot["id"] = part.get("id") or slot["id"]
                    function = part.get("function") or {}
                    slot["function"]["name"] += function.get("name") or ""
                    slot["function"]["arguments"] += function.get("arguments") or ""
                finish = choice.get("finish_reason") or finish
        return {"choices": [{"index": 0, "message": message, "finish_reason": finish}]}

    @staticmethod
    def _detail(exc: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            return ""
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else error or payload.get("message", "")
        return str(message or "")[:300]

    def _explain(self, status: int, detail: str, model: str | None, path: str) -> str:
        name = self.config.name
        if status in (401, 403):
            hint = ""
            if self.config.kind == "qwen":
                hint = f" Qwen keys only work in their own region, and this one is set to {self.config.region_label}."
            return f"The {name} key was rejected. Check that you pasted the whole key.{hint}"
        if status == 402:
            return f"Your {name} account is out of credit. Top it up, then try again."
        if status == 404:
            if path == "/models":
                return f"{name} doesn't list its models, so type the model name instead."
            return f"{name} doesn't have the model {model}. Pick another model."
        if status == 429:
            return f"{name} is rate-limiting requests. Wait a moment and try again."
        lowered = detail.lower()
        if status in (400, 422) and ("tool" in lowered or "function" in lowered):
            return f"{model or 'This model'} can't call tools, so it can't edit Blender. Pick another model for Editing."
        if status >= 500:
            return f"{name} had a server error ({status}). Try again in a moment."
        return f"{name} refused the request ({status}): {detail or 'no reason given'}"


def _seconds(value: str | None) -> float:
    try:
        return max(0.0, float(value or 1))
    except ValueError:
        return 1.0


def with_key_hint(config: ProviderConfig, key: str) -> ProviderConfig:
    return replace(config, key_hint=key[-4:])
