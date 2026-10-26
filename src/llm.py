"""统一的文本生成后端：本地开源模型权重 / 在线开源模型服务。

**为什么需要这一层（对应 AI+开源 赛题的评分要求）**

「AI 与开源融合」20 分要求说明「开源资源选型合理、能完整说明来源/版本/许可证/
自研边界/关键许可义务」。因此本模块的设计目标是：

1. **开源模型是核心组件**，而不是可选项 —— 简报生成、事实校验都由开源模型承担；
2. **两条通道都由配置切换**，不写死在代码里：
   - ``local``：从魔搭下载的开源模型权重，本地推理，可完全离线演示；
   - ``api``：任何 OpenAI 兼容端点（同样调用开源模型）；
3. **零硬编码密钥**：API 密钥只从环境变量读取。赛题规则明确禁止提交访问凭据。

本模块只负责「把提示词送进去、把文本取出来」，不含任何业务逻辑，
业务提示词与校验逻辑在 ``src/brief.py`` / ``src/verify.py``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import json
import os
import threading
import time

from . import config

LLM_CONFIG_FILE = config.CONFIG_DIR / "llm.yaml"

# 后端缓存：**按 kind 分键**。
#
# 早期版本只用一个全局变量，而且一旦显式传入 kind 就完全绕过缓存 ——
# 结果是界面里每点一次「生成简报」都新建一个 LocalTransformersBackend，
# 触发一次完整的 3B 权重加载（本机实测冷启动约 **40 秒**），
# 而且新旧两份权重会短暂同时在显存里，8 GB 卡上很容易把显存挤爆。
_cached_backends: Dict[str, "BaseBackend"] = {}
# 兼容旧调用（早期代码直接读写 _cached_backend）
_cached_backend = None


@dataclass
class GenerationResult:
    """一次生成的结果，带足够元信息用于技术报告与成本核算。"""

    text: str
    backend: str
    model: str
    elapsed_ms: int = 0
    prompt_chars: int = 0
    output_chars: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)


class BackendError(RuntimeError):
    """后端不可用（模型缺失、密钥缺失、网络失败等）。"""


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    "backend": "local",
    "local": {
        "model_id": "Qwen/Qwen2.5-3B-Instruct",
        "model_dir": "models/Qwen2.5-3B-Instruct",
        "device_map": "auto",
        "dtype": "auto",
        "load_in_4bit": True,
        "max_context": 8192,
        "max_new_tokens": 1200,
        "temperature": 0.3,
        "do_sample": True,
    },
    "api": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "api_key_env": "NEWSBRIEF_API_KEY",
        "timeout": 90,
    },
    "task": {
        # **上限**（不是目标）：实际目标 = 素材事实字数 × 0.34，被这个值封顶。
        # 见 brief.adaptive_brief_chars()。
        "brief_chars": 700,
        "brief_chars_tolerance": 0.25,
        # 标题与价值点**不设硬上限**（用户要求「正常写标题」）：0 = 不限制。
        # 实测 20 字会把「四川省华蓥市人大常委会原党组书记、主任刘」
        # 这类正常标题砍成半句。真正的护栏在 brief.HEADLINE_SOFT_MAX（48 字）。
        "headline_chars": 0,
        "value_point_chars": 0,
        "max_source_chars": 6000,
        # 草稿偏短时是否再跑一轮「定向扩写」。
        # **默认关闭**：用户要求「根据原文篇幅写简报，不要硬凑字数」，
        # 而把短的写长与这个要求直接冲突（小模型只能靠重复事实来交差）。
        "allow_expand": False,
    },
}


def load_llm_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """读取 config/llm.yaml，缺失项用默认值补齐。

    文件不存在 = 正常情况（用默认值）。但**文件存在却解析失败**必须报出来：
    默认值里的 model_id 与用户多半想改成的值相同，静默回退会表现为
    「配置改了、看起来生效了、实际没生效」，只能靠 describe() 反推，极难排查。
    """
    import yaml

    p = Path(path) if path else LLM_CONFIG_FILE
    merged = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝
    if p.exists():
        try:
            with p.open("r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception as exc:
            raise BackendError(
                f"配置文件解析失败: {p}\n{type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise BackendError(f"配置文件根节点不是映射: {p}")
        for key, value in raw.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
    return merged


def model_dir_of(cfg: Optional[Dict[str, Any]] = None) -> Path:
    cfg = cfg or load_llm_config()
    return config.PROJECT_ROOT / str(cfg["local"]["model_dir"])


# --------------------------------------------------------------------------
# 后端实现
# --------------------------------------------------------------------------

class BaseBackend:
    name = "base"

    def generate(
        self,
        prompt: str,
        system: str = "",
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> GenerationResult:
        raise NotImplementedError

    def warm(self) -> bool:
        """预先把模型加载进显存（默认无操作）。返回是否就绪。"""
        return True

    def describe(self) -> Dict[str, Any]:
        return {"backend": self.name}


class LocalTransformersBackend(BaseBackend):
    """本地加载开源模型权重（transformers）。

    两部分兼容处理，都是实际踩坑后加的：

    1. **参数名兼容**：transformers 4.56+ 把 ``torch_dtype`` 改名为 ``dtype``，
       两种都试，保证 4.x / 5.x 都能加载；
    2. **量化与回退**：3B 级权重的 bf16 体积约 5.8 GB，在 8 GB 显存上
       预填充长材料时容易 OOM。因此支持 ``load_in_4bit``（约 2 GB），
       并在量化不可用时**自动回退到全精度**，不让加载失败阻断整个流程。
    """

    name = "local"

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        self.cfg = (cfg or load_llm_config())["local"]
        self._model = None
        self._tokenizer = None
        self.load_mode = "not-loaded"
        # 加载互斥锁。启动预热线程（prewarm_async）与界面里的生成请求会命中
        # **同一个** 缓存实例，两者都会走 _ensure_loaded()。
        # `if self._model is not None` 不是原子判断：两个线程会各加载一份权重
        # （4bit 约 1.9GB×2，回退全精度则 5.8GB×2），8GB 卡上直接 OOM；
        # 更坏的情况是 A 线程刚赋了 _model，B 线程看到非空就返回，
        # 随后的 generate() 里 _tokenizer 还是 None。
        self._load_lock = threading.Lock()

    @property
    def model_dir(self) -> Path:
        return config.PROJECT_ROOT / str(self.cfg["model_dir"])

    def _load_model(self, path: str, dtype: str, device_map: str):
        """按配置尝试加载；4-bit 失败则回退全精度。"""
        import torch                                    # noqa: PLC0415
        from transformers import AutoModelForCausalLM    # noqa: PLC0415

        def _try_4bit():
            from transformers import BitsAndBytesConfig  # noqa: PLC0415
            quant = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True,
            )
            return AutoModelForCausalLM.from_pretrained(
                path, quantization_config=quant, device_map=device_map
            )

        def _try_full():
            dt = dtype
            if dt and dt != "auto":
                dt = getattr(torch, dt)
            try:
                return AutoModelForCausalLM.from_pretrained(
                    path, dtype=dt, device_map=device_map
                )
            except TypeError:
                return AutoModelForCausalLM.from_pretrained(
                    path, torch_dtype=dt, device_map=device_map
                )

        attempts = []
        if self.cfg.get("load_in_4bit"):
            attempts.append(("4bit", _try_4bit))
        attempts.append(("full", _try_full))

        errors = []
        for mode, func in attempts:
            try:
                model = func()
                self.load_mode = mode
                return model
            except Exception as exc:                    # noqa: BLE001
                errors.append(f"  {mode}: {type(exc).__name__}: {exc}")
        raise BackendError("模型加载失败（已尝试 %d 种方式）：\n%s"
                           % (len(attempts), "\n".join(errors)))

    def _ensure_loaded(self) -> None:
        # 双重检查 + 全函数持锁：确保「加载权重」这件事在进程内只发生一次。
        # 预热线程与生成线程必须串行通过这里，否则会各加载一份模型。
        if self._model is not None and self._tokenizer is not None:
            return
        with self._load_lock:
            if self._model is not None and self._tokenizer is not None:
                return
            path = self.model_dir
            if not path.exists() or not any(path.iterdir()):
                raise BackendError(
                    f"本地模型权重不存在: {path}\n"
                    f"请先执行: python scripts/fast_download.py"
                )

            from transformers import AutoTokenizer           # noqa: PLC0415

            tokenizer = AutoTokenizer.from_pretrained(str(path), use_fast=True)
            if tokenizer.pad_token_id is None:
                tokenizer.pad_token_id = tokenizer.eos_token_id

            model = self._load_model(str(path), self.cfg.get("dtype", "auto"),
                                     self.cfg.get("device_map", "auto"))
            model.eval()

            # 先赋 tokenizer 再赋 model：_model 非空是「已加载完成」的标志，
            # generate() 依赖它做早退判断，所以必须最后赋值。
            self._tokenizer = tokenizer
            self._model = model

    def generate(
        self,
        prompt: str,
        system: str = "",
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> GenerationResult:
        self._ensure_loaded()
        import torch                                    # noqa: PLC0415

        assert self._model is not None and self._tokenizer is not None

        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        tokenizer = self._tokenizer
        if getattr(tokenizer, "chat_template", None):
            text = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        else:
            text = (system + "\n\n" if system else "") + prompt + "\n"

        max_ctx = int(self.cfg.get("max_context", 8192))
        encoded = tokenizer(text, return_tensors="pt", truncation=True, max_length=max_ctx)
        model_device = next(self._model.parameters()).device
        encoded = {k: v.to(model_device) for k, v in encoded.items()}

        gen_kwargs: Dict[str, Any] = {
            "max_new_tokens": int(max_new_tokens or self.cfg.get("max_new_tokens", 768)),
            "do_sample": bool(self.cfg.get("do_sample", True)),
            "pad_token_id": tokenizer.pad_token_id,
        }
        temp = temperature if temperature is not None else float(self.cfg.get("temperature", 0.3))
        if gen_kwargs["do_sample"] and temp > 0:
            gen_kwargs["temperature"] = temp

        t0 = time.time()
        with torch.no_grad():
            output_ids = self._model.generate(**encoded, **gen_kwargs)
        elapsed = int((time.time() - t0) * 1000)

        new_tokens = output_ids[0][encoded["input_ids"].shape[-1]:]
        text_out = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()

        return GenerationResult(
            text=text_out,
            backend=self.name,
            model=str(self.cfg.get("model_id", "")),
            elapsed_ms=elapsed,
            prompt_chars=len(prompt),
            output_chars=len(text_out),
            extra={
                "device": str(model_device),
                "load_mode": self.load_mode,
                "input_tokens": int(encoded["input_ids"].shape[-1]),
                "output_tokens": int(new_tokens.shape[-1]),
                "weights_dir": str(self.model_dir),
            },
        )

    def warm(self) -> bool:
        """把权重加载进显存（首次调用很慢，之后就是空操作）。"""
        self._ensure_loaded()
        return self._model is not None

    def describe(self) -> Dict[str, Any]:
        return {
            "backend": self.name,
            "model_id": self.cfg.get("model_id"),
            "weights_dir": str(self.model_dir),
            "load_mode": self.load_mode,
            "load_in_4bit": bool(self.cfg.get("load_in_4bit")),
            "weights_present": self.model_dir.exists() and any(self.model_dir.iterdir())
            if self.model_dir.exists() else False,
            "device_map": self.cfg.get("device_map"),
        }


class OpenAICompatBackend(BaseBackend):
    """调用 OpenAI 兼容端点的开源模型服务。

    密钥只从环境变量读取；缺失时抛出明确错误，绝不回退到硬编码密钥。
    """

    name = "api"

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        self.cfg = (cfg or load_llm_config())["api"]

    @property
    def api_key(self) -> str:
        env_name = str(self.cfg.get("api_key_env", "NEWSBRIEF_API_KEY"))
        key = os.environ.get(env_name, "").strip()
        if not key:
            raise BackendError(
                f"未找到 API 密钥。请设置环境变量 {env_name} 后重试：\n"
                f'  PowerShell:  $env:{env_name}="sk-..."\n'
                f"密钥只从环境变量读取，不会写入仓库或日志。"
            )
        return key

    def generate(
        self,
        prompt: str,
        system: str = "",
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> GenerationResult:
        import requests                                 # noqa: PLC0415

        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: Dict[str, Any] = {
            "model": self.cfg.get("model"),
            "messages": messages,
            "temperature": temperature if temperature is not None
            else float(self.cfg.get("temperature", 0.3)),
        }
        if max_new_tokens:
            payload["max_tokens"] = int(max_new_tokens)

        base = str(self.cfg.get("base_url", "")).rstrip("/")
        t0 = time.time()
        try:
            resp = requests.post(
                f"{base}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                timeout=int(self.cfg.get("timeout", 90)),
            )
        except requests.RequestException as exc:
            raise BackendError(f"调用在线模型服务失败: {type(exc).__name__}: {exc}") from exc

        elapsed = int((time.time() - t0) * 1000)
        if resp.status_code >= 400:
            raise BackendError(
                f"在线模型服务返回 {resp.status_code}: {resp.text[:300]}"
            )

        data = resp.json()
        text_out = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage") or {}
        return GenerationResult(
            text=(text_out or "").strip(),
            backend=self.name,
            model=str(data.get("model") or self.cfg.get("model")),
            elapsed_ms=elapsed,
            prompt_chars=len(prompt),
            output_chars=len(text_out or ""),
            extra={
                "base_url": base,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
            },
        )

    def describe(self) -> Dict[str, Any]:
        env_name = str(self.cfg.get("api_key_env", "NEWSBRIEF_API_KEY"))
        return {
            "backend": self.name,
            "base_url": self.cfg.get("base_url"),
            "model": self.cfg.get("model"),
            "api_key_env": env_name,
            "api_key_present": bool(os.environ.get(env_name, "").strip()),
        }


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------

def build_backend(cfg: Optional[Dict[str, Any]] = None, kind: Optional[str] = None) -> BaseBackend:
    """按配置构建后端。kind 可显式覆盖配置里的 backend 字段。"""
    cfg = cfg or load_llm_config()
    which = (kind or cfg.get("backend") or "local").lower()
    if which == "api":
        return OpenAICompatBackend(cfg)
    if which == "local":
        return LocalTransformersBackend(cfg)
    raise ValueError(f"未知的 backend: {which}（应为 local 或 api）")


def get_backend(cfg: Optional[Dict[str, Any]] = None, kind: Optional[str] = None,
                reuse: bool = True) -> BaseBackend:
    """获取后端，**按 kind 复用同一实例**（避免重复加载模型权重）。

    缓存必须按 kind 分键：早期版本写成「只有 kind 为 None 才走缓存」，
    而界面永远会传 kind（单选框总有值），于是缓存完全失效。
    """
    global _cached_backend
    cfg = cfg or load_llm_config()
    which = (kind or cfg.get("backend") or "local").lower()
    if reuse:
        hit = _cached_backends.get(which)
        if hit is not None:
            return hit
    backend = build_backend(cfg, which)
    if reuse:
        _cached_backends[which] = backend
        _cached_backend = backend          # 兼容旧引用
    return backend


def prewarm_async(cfg: Optional[Dict[str, Any]] = None,
                  kind: Optional[str] = None) -> "threading.Thread":
    """在守护线程里预热权重，避免用户第一次点「生成简报」干等四十秒。

    界面启动时就调一次：模型加载与「打开网页」这件事在时间上重叠，
    用户先看界面说明、选事件，等真正点生成时权重通常已经就绪。
    """
    import threading                                # noqa: PLC0415

    def _run() -> None:
        t0 = time.time()
        try:
            backend = get_backend(cfg, kind)
            backend.warm()
            print(f"[LLM] 权重预热完成（{time.time() - t0:.1f}s）", flush=True)
        except Exception as exc:                    # noqa: BLE001
            print(f"[LLM] 权重预热失败（不影响后续使用）："
                  f"{type(exc).__name__}: {exc}", flush=True)

    thread = threading.Thread(target=_run, name="llm-prewarm", daemon=True)
    thread.start()
    return thread
