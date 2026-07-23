"""
llm_client.py — 可插拔 LLM / Embedding 后端（Biomni-E1 Phase 2/3 复用）

设计目标：
  * 支持 OpenAI 兼容接口（默认 OpenAI，可指向任意兼容网关，如本地 vLLM / Ollama / 第三方）
  * 未配置 API key 时自动降级为 stub：extract() 返回空实体，embed() 返回确定性哈希向量
    ——这样整条流水线（Phase 1→5）无需付费即可端到端跑通，便于演示与调试
  * 所有请求走纯 requests，不强制依赖 openai 包

环境变量：OPENAI_API_KEY（或 llm_config.json 中 api_key_env 指定的变量）
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "llm_config.json"


def _load_config() -> dict:
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    return {}


class LLMClient:
    def __init__(self, config: dict | None = None):
        self.cfg = config or _load_config()
        self.backend = self.cfg.get("backend", "openai_compatible")
        self.base_url = self.cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
        self.model = self.cfg.get("model", "gpt-4o")
        self.temperature = float(self.cfg.get("temperature", 0.0))
        self.max_tokens = int(self.cfg.get("max_tokens", 4096))
        self.timeout = int(self.cfg.get("request_timeout", 120))
        self.max_retries = int(self.cfg.get("max_retries", 2))
        self.stub = bool(self.cfg.get("stub_when_no_key", True))

        api_key_env = self.cfg.get("api_key_env", "OPENAI_API_KEY")
        self.api_key = os.environ.get(api_key_env, "")
        if not self.api_key:
            # 也兼容直接写在配置文件里（不推荐，仅演示）
            self.api_key = self.cfg.get("api_key", "")
        self._using_stub = self.stub and not self.api_key

        emb = self.cfg.get("embedding", {})
        self.emb_backend = emb.get("backend", "openai_compatible")
        self.emb_model = emb.get("model", "text-embedding-3-large")
        self.emb_base_url = emb.get("base_url", self.base_url).rstrip("/")
        self.emb_api_key = os.environ.get(emb.get("api_key_env", api_key_env), "") or self.api_key
        self._st_model = None  # 懒加载的 sentence_transformers 模型

    # ---- 文本生成 + JSON 提取 ----
    def extract(self, system_prompt: str, user_prompt: str) -> dict:
        """调用 LLM 提取四类实体，返回 dict（含 tasks/tools/databases/software）。"""
        if self._using_stub:
            return self._stub_extract(user_prompt)
        for attempt in range(self.max_retries + 1):
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "temperature": self.temperature,
                        "max_tokens": self.max_tokens,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "response_format": {"type": "json_object"},
                    },
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                return self._safe_parse(content)
            except Exception as e:  # noqa: BLE001
                if attempt >= self.max_retries:
                    sys.stderr.write(f"[LLMClient] extract failed: {e}\n")
                    return self._stub_extract(user_prompt, failed=True)
                time.sleep(2 * (attempt + 1))

    # ---- Embedding ----
    def embed(self, texts: list[str]) -> list[list[float]]:
        """返回与 texts 等长的向量列表。

        - emb_backend == "sentence_transformers": 本地模型编码（无需 key）
        - openai_compatible + 有 key: 走远程 /embeddings
        - 其余: 确定性 64 维占位向量（仅供流程贯通，非语义）
        """
        if not texts:
            return []
        if self.emb_backend == "sentence_transformers":
            model = self._load_st_model()
            if model is not None:
                try:
                    vecs = model.encode(texts, normalize_embeddings=True)
                    return [list(map(float, v)) for v in vecs]
                except Exception as e:  # noqa: BLE001
                    sys.stderr.write(f"[LLMClient] sentence_transformers encode failed: {e}\n")
            return [self._hash_vec(t, dim=64) for t in texts]
        if self.stub and not self.emb_api_key:
            return [self._hash_vec(t, dim=64) for t in texts]
        for attempt in range(self.max_retries + 1):
            try:
                resp = requests.post(
                    f"{self.emb_base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.emb_api_key}",
                             "Content-Type": "application/json"},
                    json={"model": self.emb_model, "input": texts},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()["data"]
                return [d["embedding"] for d in sorted(data, key=lambda x: x["index"])]
            except Exception as e:  # noqa: BLE001
                if attempt >= self.max_retries:
                    sys.stderr.write(f"[LLMClient] embed failed: {e}\n")
                    return [self._hash_vec(t, dim=64) for t in texts]
                time.sleep(2 * (attempt + 1))

    # ---- 本地 embedding 后端（sentence_transformers）----
    def _load_st_model(self):
        if self._st_model is not None:
            return self._st_model
        try:
            from sentence_transformers import SentenceTransformer
            sys.stderr.write(f"[LLMClient] 加载本地 embedding 模型 {self.emb_model} ...\n")
            self._st_model = SentenceTransformer(self.emb_model)
            return self._st_model
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[LLMClient] sentence_transformers 不可用: {e}\n")
            return None

    def has_embeddings(self) -> bool:
        """是否有可用的语义 embedding 后端（本地模型或远程 key）。决定 Layer 3/检索是否语义化。"""
        if self.emb_backend == "sentence_transformers":
            return self._load_st_model() is not None
        return bool(self.emb_api_key)

    # ---------- stub / 解析辅助 ----------
    @staticmethod
    def _safe_parse(content: str) -> dict:
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            # 容错：抠出第一个 {...} 块
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end != -1:
                try:
                    obj = json.loads(content[start:end + 1])
                except json.JSONDecodeError:
                    return {"tasks": [], "tools": [], "databases": [], "software": [],
                            "parse_error": True}
            else:
                return {"tasks": [], "tools": [], "databases": [], "software": [],
                        "parse_error": True}
        for k in ("tasks", "tools", "databases", "software"):
            obj.setdefault(k, [])
        return obj

    @staticmethod
    def _stub_extract(user_prompt: str, failed: bool = False) -> dict:
        # 无 key / 失败时的确定性占位：不编造实体，仅保留元信息，便于后续人工补
        return {
            "tasks": [], "tools": [], "databases": [], "software": [],
            "_stub": True,
            "_reason": "no_api_key" if not failed else "llm_error",
        }

    @staticmethod
    def _hash_vec(text: str, dim: int = 64) -> list[float]:
        # 确定性、与语义无关的占位向量，仅供流程贯通；真实去重请配置 embedding 后端
        vec = []
        for i in range(dim):
            h = hashlib.md5(f"{text}::{i}".encode("utf-8")).digest()
            val = (int.from_bytes(h[:4], "big") / 0xFFFFFFFF) * 2 - 1
            vec.append(val)
        norm = (sum(v * v for v in vec)) ** 0.5 or 1.0
        return [v / norm for v in vec]

    def status(self) -> str:
        return "stub (no API key)" if self._using_stub else f"live ({self.model})"


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。维度不一致或为空返回 0.0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


if __name__ == "__main__":
    c = LLMClient()
    print("LLM backend:", c.status())
    out = c.extract("你是一个测试助手。", "返回 {\"ok\": true}")
    print("extract sample:", out)
    print("embed dim:", len(c.embed(["hello world"])[0]))
