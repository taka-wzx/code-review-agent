"""Provider-agnostic LLM client construction (split out of agent.py).

Both DeepSeek and Zhipu/GLM expose OpenAI-compatible endpoints, so one
client works for both -- only the base_url, model id, and key env var
change. Pick the provider with LLM_PROVIDER:
    deepseek (default)  needs DEEPSEEK_API_KEY
    glm                 needs GLM_API_KEY (or ZHIPUAI_API_KEY)
"""
import os
import sys
from pathlib import Path
from typing import TypedDict

from openai import OpenAI


class ProviderConfig(TypedDict):
    base_url: str
    model: str
    key_envs: tuple[str, ...]


PROVIDERS: dict[str, ProviderConfig] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-pro",
        "key_envs": ("DEEPSEEK_API_KEY",),
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.6",
        "key_envs": ("GLM_API_KEY", "ZHIPUAI_API_KEY"),
    },
}
REQUEST_TIMEOUT = 120.0  # seconds per API call
MAX_SECRET_FILE_BYTES = 4096


def _api_key_from_environment(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    for name in names:
        path_value = os.environ.get(f"{name}_FILE")
        if not path_value:
            continue
        try:
            encoded = Path(path_value).read_bytes()
        except OSError as exc:
            raise RuntimeError(f"{name}_FILE is unavailable") from exc
        if len(encoded) > MAX_SECRET_FILE_BYTES:
            raise RuntimeError(f"{name}_FILE exceeds the supported size")
        try:
            value = encoded.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"{name}_FILE is not UTF-8") from exc
        if not value:
            raise RuntimeError(f"{name}_FILE is empty")
        return value
    return None


def load_dotenv() -> None:
    """Load KEY=VALUE lines from ./.env (real env vars win).

    Resolved against the current working directory, not this file: once the
    package is pip-installed, __file__ lives in site-packages where no .env
    will ever be."""
    env_file = Path.cwd() / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def make_client(*, load_env_file: bool = True) -> tuple[OpenAI, str]:
    """Build a provider-agnostic client + model id from LLM_PROVIDER env.

    LLM_MODEL overrides the provider's default model id -- e.g. to pin a
    dated snapshot for reproducible evals, or to try another model on the
    same endpoint. Note the defaults above are provider ALIASES the vendor
    can repoint at new weights; cross-run comparisons should record the
    model id (traces do) and treat alias drift as a confound."""
    if load_env_file:
        load_dotenv()
    provider = os.environ.get("LLM_PROVIDER", "deepseek").lower()
    if provider not in PROVIDERS:
        sys.exit(f"Unknown LLM_PROVIDER={provider!r}; choose one of {list(PROVIDERS)}")
    cfg = PROVIDERS[provider]
    try:
        api_key = _api_key_from_environment(cfg["key_envs"])
    except RuntimeError as exc:
        sys.exit(str(exc))
    if not api_key:
        envs = " or ".join(cfg["key_envs"])
        sys.exit(
            f"No credentials for provider {provider!r}: set {envs} or the corresponding "
            "_FILE variable"
        )
    client = OpenAI(api_key=api_key, base_url=cfg["base_url"],
                    timeout=REQUEST_TIMEOUT, max_retries=2)
    return client, os.environ.get("LLM_MODEL") or cfg["model"]
