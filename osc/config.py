"""Configuration: paths, domains, curated seeds, scoring weights, agent settings.

Everything here is data. Override any scalar via env var OSC_<NAME> (e.g. OSC_MIN_STARS=500)
or via data/settings.json (edited from the UI).
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = DATA_DIR / "reports"
WORKSPACE_DIR = ROOT / "workspace"
LOGS_DIR = ROOT / "logs"
PROMPTS_DIR = ROOT / "prompts"
UI_DIR = ROOT / "ui"
DB_PATH = DATA_DIR / "osc.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
VENV_BIN = ROOT / ".venv" / "bin"

for _d in (DATA_DIR, REPORTS_DIR, WORKSPACE_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------------------
# Tunables (env / settings.json override)
# --------------------------------------------------------------------------------------
DEFAULTS = {
    "MIN_STARS": 1000,            # "well known" floor
    "MAX_STARS": 200000,
    "PUSHED_WITHIN_DAYS": 45,     # must be actively maintained
    "MAX_OPEN_PRS_SOFT": 400,     # crowding penalty starts here
    "MAX_OPEN_PRS_HARD": 2500,    # above this, merge odds for outsiders are poor
    "SEARCH_PER_QUERY": 30,       # GitHub search page size per query
    "TOP_N_TO_ANALYZE": 12,       # default number of repos the analyze stage takes
    "CLONE_DEPTH": 1,
    "SCOUT_MODEL": "sonnet",
    "BUILDER_MODEL": "opus",
    "REVIEWER_MODEL": "sonnet",
    "SCOUT_MAX_TURNS": 60,
    "SCOUT_TIMEOUT_S": 1500,
    "SCOUT_BUDGET_USD": 3.5,
    "BUILDER_BUDGET_USD": 12.0,   # first builder round
    "REVISE_BUDGET_USD": 6.0,     # each later round
    "REVISE_MODEL": "sonnet",     # later rounds only apply the reviewer's listed fixes
    "REVIEWER_BUDGET_USD": 3.0,
    "COMPLIANCE_BUDGET_USD": 2.0,
    # ---- autonomous daily run ----
    "DAILY_MAX_REPOS": 12,         # repos scouted per run (two runs a day)
    "DAILY_MAX_BUILDS": 10,        # upper bound on builds per day; building stops as soon as DAILY_TARGET_PRS are open
    "DAILY_BUDGET_USD": 100.0,     # hard stop for agent spend per day (both runs combined)
    # ---- auto-open (user requirement 2026-09-29: at least one PR a day, strong repos only) ----
    "DAILY_BACKLOG_MAX_AGE_DAYS": 21,  # unbuilt opportunities from earlier scouts stay eligible this long
    "DAILY_MIN_PRS": 3,           # user 10-05: the quota keeper hunts (spends rescue money) until this many PRs are open today
    "DAILY_TARGET_PRS": 10,       # user 10-05: hard cap; never open more than this many PRs in one day
    "DAILY_RESCUE_USD": 200.0,     # extra spend per day for the hourly quota keeper while under DAILY_TARGET_PRS
    "DAILY_RESCUE_ROUNDS": 4,     # extra scout+build rounds per run when the quota is unmet
    "DAILY_RESCUE_BUILDS": 3,     # builds per rescue round
    "AUTO_OPEN": True,            # open PRs for changes that pass every gate in osc/opener.py
    "OPEN_MIN_REVIEW": 7.0,       # reviewer score floor (verdict must be approve)
    "OPEN_MIN_LIKELIHOOD": 6,     # compliance agent merge-likelihood floor (1-10)
    "OPEN_MAX_AGE_DAYS": 21,      # older prepared branches are rebuilt, not opened
    "STRONG_MIN_STARS": 2000,
    "STRONG_MIN_COMMITS_90D": 30,
    "STRONG_MIN_EXT_MERGE": 0.25, # share of outside-contributor PRs that get merged
    "CLA_SIGNED": ["google"],     # CLA families already signed; others are not auto-opened until signed
    # ---- follow-up on open PRs (user requirement 2026-10-04: answer feedback and push fixes without a human) ----
    "RESPOND_ENABLED": True,
    "RESPOND_DELAY_HOURS": 2.0,   # leave feedback alone this long after the newest comment (reviewers post in batches)
    "RESPOND_MAX_WAIT_HOURS": 10.0,  # but answer once the oldest unanswered item is this old, even if talk continues
    "RESPOND_MAX_AGE_DAYS": 14,   # never dig up feedback older than this
    "RESPOND_CI": True,           # also fix CI that goes red on our head commit
    "RESPOND_ALLOW_CLOSE": True,  # close our PR (with a short thanks) when a maintainer says it will not be merged
    "RESPOND_MODEL": "opus",
    "RESPOND_MAX_TURNS": 150,
    "RESPOND_BUDGET_USD": 8.0,    # per agent attempt on one PR
    "RESPOND_VERIFY_BUDGET_USD": 2.0,
    "RESPOND_DAILY_USD": 60.0,    # all follow-up work per day (own ledger in data/respond_state.json)
    "RESPOND_MAX_ROUNDS_PER_PR_DAY": 3,
    "RESPOND_REREQUEST_DAYS": 3,  # re-request review once, this long after our push made a maintainer's review stale
    # ---- Discussions answers toward the Galaxy Brain badge (user goal 2026-10-04) ----
    "DISCUSS_AUTO_POST": True,    # user 10-04: "whatever you think is best" -> on, checker-gated, max DISCUSS_PER_RUN a day
    "DISCUSS_DISCLOSURE": "(I looked this up in the code with help from an AI assistant; the links are to the lines I checked.)",
    "DISCUSS_MODEL": "sonnet",
    # ---- maintenance of the owner's own public repos (user requirement 2026-10-04) ----
    "MAINTAIN_MODEL": "opus",
    "MAINTAIN_BUDGET_USD": 5.0,
    "MAINTAIN_DAILY": ["skilljail"],   # user 10-05: maintained every day, on top of the rotation
    "DISCUSS_BUDGET_USD": 2.5,
    "DISCUSS_DAILY_USD": 40.0,     # user 10-05: push for Galaxy Brain; runs every 2 hours
    "DISCUSS_PER_RUN": 2,         # answers per run, a run every 2 hours; every answer still has to pass the checker
    "DISCUSS_MAX_PER_DAY": 8,     # posted answers per day; more reads as a bot to GitHub and to maintainers
    "DISCUSS_EXTRA_REPOS": 150,    # best-ranked repos in our domains, on top of the ones we contribute to
    "DISCUSS_MIN_ACCEPT_RATE": 0.25,  # skip repos where askers rarely mark answers
    "DISCUSS_MAX_AGE_DAYS": 21,
    "DAILY_RANK_DEPTH": 400,      # how far down each domain ranking the picker looks (60 ran dry 10-06: 0 eligible, 355 at 400)
    "DAILY_HELPWANTED_REPOS": 4,  # user 10-06: slots per run for projects that ask for outside help (osc/helpwanted.py)
    "HELPWANTED_MIN_ISSUES": 2,   # a project must have at least this many fresh, unassigned help-wanted issues
    # ---- advisory-database corrections (user 2026-10-06, Security advisory credit badge) ----
    "ADVISORY_AUTO_SUBMIT": True,   # user 2026-10-06: scheduled job submits on its own after self-verification
    "ADVISORY_MODEL": "opus",
    "ADVISORY_BUDGET_USD": 3.0,
    "ADVISORY_MAX_PER_DAY": 3,      # cap PRs to github/advisory-database per day (a flood reads as spam)
    # ---- responsible disclosure of non-public security findings (route 2) ----
    "DISCLOSE_CHECK": True,         # classify every security change before a public PR
    "DISCLOSE_AUTO_SUBMIT": False,  # never auto-send a private report; the user confirms each one
    "DISCLOSE_MODEL": "opus",
    "DISCLOSE_BUDGET_USD": 2.0,
    "DAILY_DOMAINS": ["llm-inference", "post-training", "security", "security-vendors", "agents-mcp", "ml-core", "crypto-pq"],
    "DAILY_MIN_PRIORITY": 0.6,    # opportunity priority floor to build
    "DAILY_MIN_ACCEPT": 0.5,      # scout's accept-likelihood floor to build
    "DAILY_REANALYZE_DAYS": 10,   # do not re-scout a repo more often than this
    "DAILY_RESCAN_DAYS": 3,       # refresh GitHub discovery/scores this often
    "MONTHLY_BUDGET_USD": 5000,  # rolling 30 days; above this only PR states are refreshed
    "SCOUT_PARALLEL": 3,
    "DAILY_DIGEST_TO": "",           # set in data/settings.json or OSC_DAILY_DIGEST_TO; empty = digests stay on disk
    "MAX_WORKSPACE_GB": 25.0,     # clones + build artifacts cap
    "MIN_FREE_GB": 12.0,          # skip builds below this much free disk
    "BUILDER_MAX_TURNS": 200,
    "REVIEWER_MAX_TURNS": 40,
    "MAX_REVIEW_ROUNDS": 3,       # builder <-> reviewer iterations
    "AI_COAUTHOR_TRAILER": True,  # keep Claude Code's Co-authored-by trailer (disclosure); False strips it
    "MIN_QUALITY_SCORE": 85,      # AI-pattern lint gate (0-100); below this a change cannot become 'ready'
    "MIN_DIFF_LINES": 25,         # below this a change is "trivial" and gets flagged
    "MAX_DIFF_LINES": 900,        # above this a change is too big for a first PR
    "LANG_WEIGHTS": {
        "Python": 1.0, "TypeScript": 0.85, "JavaScript": 0.7, "Rust": 0.8, "C++": 0.8,
        "C": 0.75, "Cuda": 0.7, "Go": 0.7, "Jupyter Notebook": 0.3, "Shell": 0.4,
        "Java": 0.5, "Kotlin": 0.4, "Swift": 0.5, "Zig": 0.5, "Mojo": 0.5, "Julia": 0.4,
    },
    "SCORE_WEIGHTS": {
        "domain_fit": 0.20, "popularity": 0.08, "activity": 0.15, "openness": 0.22,
        "approachability": 0.15, "language_fit": 0.10, "crowding": 0.10,
    },
}


def _load_settings_file() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except Exception:
        return {}


def setting(name: str):
    env = os.environ.get(f"OSC_{name}")
    if env is not None:
        base = DEFAULTS.get(name)
        if isinstance(base, bool):
            return env.lower() in ("1", "true", "yes")
        if isinstance(base, int):
            return int(env)
        if isinstance(base, float):
            return float(env)
        if isinstance(base, (dict, list)):
            return json.loads(env)
        return env
    file_settings = _load_settings_file()
    if name in file_settings:
        return file_settings[name]
    return DEFAULTS[name]


def save_settings(patch: dict) -> dict:
    cur = _load_settings_file()
    cur.update({k: v for k, v in patch.items() if k in DEFAULTS})
    SETTINGS_PATH.write_text(json.dumps(cur, indent=2))
    return cur


def all_settings() -> dict:
    return {k: setting(k) for k in DEFAULTS}


# --------------------------------------------------------------------------------------
# Domains: what we want to contribute to. Each has GitHub topics, search phrases, and
# curated seed repos (verified live by the scanner, never trusted blindly).
# --------------------------------------------------------------------------------------
DOMAINS: dict[str, dict] = {
    "help-wanted": {
        "label": "Projects asking for outside help",
        "topics": [], "keywords": [], "seeds": [],
    },
    "tier1": {
        "label": "Flagship projects (resume tier)",
        "topics": ["kernel", "compiler", "browser-engine", "deep-learning", "kubernetes", "programming-language"],
        "keywords": ["kernel", "compiler", "interpreter", "runtime", "scheduler", "browser", "standard library"],
        "seeds": [
            "torvalds/linux", "llvm/llvm-project", "pytorch/pytorch", "python/cpython", "kubernetes/kubernetes",
            "vllm-project/vllm", "golang/go", "WebKit/WebKit", "huggingface/diffusers", "openssl/openssl",
        ],
    },
    "llm-inference": {
        "label": "LLM inference & serving",
        "topics": ["llm-inference", "llm-serving", "inference", "inference-engine", "kv-cache",
                   "quantization", "speculative-decoding", "cuda", "tensorrt", "gguf", "vllm"],
        "keywords": ["inference", "serving", "quantiz", "kv cache", "kv-cache", "speculative",
                     "throughput", "tokens/s", "llama.cpp", "gguf", "attention kernel", "paged"],
        "seeds": [
            "vllm-project/vllm", "sgl-project/sglang", "ggml-org/llama.cpp", "NVIDIA/TensorRT-LLM",
            "huggingface/text-generation-inference", "InternLM/lmdeploy", "mlc-ai/mlc-llm",
            "ollama/ollama", "turboderp-org/exllamav3", "turboderp/exllamav2", "OpenNMT/CTranslate2",
            "predibase/lorax", "ModelTC/LightLLM", "flashinfer-ai/flashinfer", "Dao-AILab/flash-attention",
            "huggingface/candle", "ml-explore/mlx", "ml-explore/mlx-lm", "microsoft/onnxruntime-genai",
            "intel/ipex-llm", "NVIDIA/TensorRT-Model-Optimizer", "vllm-project/llm-compressor",
            "huggingface/optimum", "pytorch/ao", "microsoft/BitNet", "bentoml/OpenLLM",
            "xorbitsai/inference", "lm-sys/FastChat", "mudler/LocalAI", "Mozilla-Ocho/llamafile",
            "triton-lang/triton", "kvcache-ai/ktransformers", "LMCache/LMCache", "ai-dynamo/dynamo",
            "ggml-org/ggml", "mit-han-lab/llm-awq", "casper-hansen/AutoAWQ", "ModelCloud/GPTQModel",
            "huggingface/text-embeddings-inference", "vllm-project/vllm-ascend", "PygmalionAI/aphrodite-engine",
            "mlc-ai/web-llm", "b4rtaz/distributed-llama", "exo-explore/exo", "llm-d/llm-d",
            "vllm-project/production-stack", "NVIDIA/cutlass", "ROCm/aiter", "tile-ai/tilelang",
                    "abetlen/llama-cpp-python", "ggml-org/whisper.cpp", "EricLBuehler/mistral.rs", "tracel-ai/burn", "Lightning-AI/litserve", "bentoml/BentoML", "kserve/kserve", "openai/tiktoken", "intel/neural-compressor", "huggingface/optimum-quanto",
        ],
    },
    "post-training": {
        "label": "Post-training, RLHF, fine-tuning, eval",
        "topics": ["rlhf", "fine-tuning", "finetuning", "post-training", "reinforcement-learning",
                   "alignment", "dpo", "grpo", "lora", "peft", "llm-training", "distributed-training",
                   "llm-evaluation"],
        "keywords": ["rlhf", "fine-tun", "finetun", "post-training", "dpo", "grpo", "ppo", "lora",
                     "reward model", "alignment", "sft", "instruction tuning", "evaluation harness"],
        "seeds": [
            "huggingface/trl", "OpenRLHF/OpenRLHF", "volcengine/verl", "axolotl-ai-cloud/axolotl",
            "hiyouga/LLaMA-Factory", "unslothai/unsloth", "pytorch/torchtune", "huggingface/peft",
            "allenai/open-instruct", "huggingface/alignment-handbook", "NVIDIA-NeMo/RL", "NVIDIA/NeMo",
            "PRIME-RL/PRIME", "rllm-org/rllm", "deepspeedai/DeepSpeed", "NVIDIA/Megatron-LM",
            "huggingface/accelerate", "huggingface/nanotron", "microsoft/LMOps", "Lightning-AI/litgpt",
            "oumi-ai/oumi", "arcee-ai/mergekit", "huggingface/datatrove", "huggingface/lighteval",
            "EleutherAI/lm-evaluation-harness", "UKGovernmentBEIS/inspect_ai", "huggingface/open-r1",
            "THUDM/slime", "skypilot-org/skypilot", "ray-project/ray", "pytorch/torchtitan",
            "huggingface/smolagents", "MoonshotAI/checkpoint-engine", "inclusionAI/AReaL",
            "OpenPipe/ART", "willccbb/verifiers", "PrimeIntellect-ai/prime-rl", "huggingface/trackio",
            "pytorch/torchforge", "openai/evals", "confident-ai/deepeval", "explodinggradients/ragas",
            "stanfordnlp/dspy", "huggingface/transformers", "huggingface/datasets", "huggingface/tokenizers",
            "bitsandbytes-foundation/bitsandbytes", "facebookresearch/xformers", "Lightning-AI/pytorch-lightning",
            "mosaicml/llm-foundry", "microsoft/DeepSpeed-MII", "NVIDIA/NeMo-Guardrails",
                    "stanford-crfm/helm", "EleutherAI/gpt-neox", "facebookresearch/fairseq2", "huggingface/evaluate", "pytorch/torchchat",
        ],
    },
    "security": {
        "label": "Security (AI security, appsec, offensive/defensive tooling)",
        "topics": ["security", "cybersecurity", "llm-security", "ai-security", "prompt-injection",
                   "red-teaming", "vulnerability-scanner", "sast", "pentesting", "fuzzing",
                   "reverse-engineering", "malware-analysis", "threat-detection", "supply-chain-security"],
        "keywords": ["security", "vulnerab", "prompt injection", "jailbreak", "red team", "guardrail",
                     "scanner", "exploit", "fuzz", "malware", "threat", "cve", "sast", "dast", "secrets"],
        "seeds": [
            "NVIDIA/garak", "Azure/PyRIT", "promptfoo/promptfoo", "protectai/llm-guard",
            "NVIDIA/NeMo-Guardrails", "meta-llama/PurpleLlama", "guardrails-ai/guardrails",
            "Trusted-AI/adversarial-robustness-toolbox", "mitre/caldera", "redcanaryco/atomic-red-team",
            "semgrep/semgrep", "aquasecurity/trivy", "projectdiscovery/nuclei", "zaproxy/zaproxy",
            "sqlmapproject/sqlmap", "rapid7/metasploit-framework", "ossf/scorecard", "google/osv-scanner",
            "anchore/grype", "anchore/syft", "gitleaks/gitleaks", "trufflesecurity/trufflehog",
            "PyCQA/bandit", "falcosecurity/falco", "wazuh/wazuh", "SigmaHQ/sigma", "owasp-amass/amass",
            "bridgecrewio/checkov", "protectai/modelscan", "huggingface/safetensors", "google/oss-fuzz",
            "AFLplusplus/AFLplusplus", "angr/angr", "pwndbg/pwndbg", "radareorg/radare2",
            "NationalSecurityAgency/ghidra", "frida/frida", "google/syzkaller", "cure53/DOMPurify",
            "invariantlabs-ai/invariant", "lakeraai/pint-benchmark", "usnistgov/dioptra",
            "Giskard-AI/giskard", "confident-ai/deepteam", "mindersec/minder", "sigstore/cosign",
            "in-toto/in-toto", "slsa-framework/slsa", "cisagov/ScubaGear", "OWASP/Nettacker",
            "projectdiscovery/katana", "projectdiscovery/subfinder", "projectdiscovery/httpx",
            "BishopFox/sliver", "caido/caido", "nmap/nmap", "wireshark/wireshark", "zeek/zeek",
            "elastic/detection-rules", "CycloneDX/cyclonedx-python", "guardsquare? ".strip("? "),
            "MobSF/Mobile-Security-Framework-MobSF", "OWASP/CheatSheetSeries", "danielmiessler/Fabric",
            "google/security-research", "microsoft/presidio", "ProtectAI/vulnhuntr", "openai/gpt-oss-safeguard",
            "anthropics/courses", "usnistgov/ACVP", "openssl/openssl", "python/cpython",
                    "crowdsecurity/crowdsec", "authelia/authelia", "oauth2-proxy/oauth2-proxy", "smallstep/certificates", "cloudflare/cfssl", "certbot/certbot", "getsops/sops", "FiloSottile/age", "tailscale/tailscale", "juanfont/headscale", "WireGuard/wireguard-go", "open-policy-agent/opa", "kyverno/kyverno", "aquasecurity/tracee", "aquasecurity/kube-bench", "osquery/osquery", "google/tink", "google/gvisor", "firecracker-microvm/firecracker", "VirusTotal/yara-x", "volatilityfoundation/volatility3", "mandiant/capa", "fortra/impacket", "OISF/suricata", "goauthentik/authentik", "ory/kratos", "Velocidex/velociraptor", "greenbone/openvas-scanner", "hashicorp/vault", "OpenCTI-Platform/opencti",
        ],
    },
    "agents-mcp": {
        "label": "Agents, MCP, LLM apps & tooling",
        "topics": ["llm-agents", "agents", "mcp", "model-context-protocol", "ai-agents", "agentic",
                   "llm-framework", "rag", "llmops", "coding-agent", "computer-use"],
        "keywords": ["agent", "mcp", "model context protocol", "tool use", "tool calling", "rag",
                     "orchestrat", "coding assistant", "computer use", "browser automation"],
        "seeds": [
            "modelcontextprotocol/servers", "modelcontextprotocol/python-sdk", "modelcontextprotocol/typescript-sdk",
            "modelcontextprotocol/inspector", "langchain-ai/langchain", "langchain-ai/langgraph",
            "run-llama/llama_index", "microsoft/autogen", "crewAIInc/crewAI", "huggingface/smolagents",
            "All-Hands-AI/OpenHands", "browser-use/browser-use", "openai/openai-agents-python",
            "google/adk-python", "pydantic/pydantic-ai", "letta-ai/letta", "Significant-Gravitas/AutoGPT",
            "block/goose", "SWE-agent/SWE-agent", "Aider-AI/aider", "microsoft/semantic-kernel",
            "deepset-ai/haystack", "BerriAI/litellm", "567-labs/instructor", "dottxt-ai/outlines",
            "guidance-ai/guidance", "mem0ai/mem0", "langfuse/langfuse", "Arize-ai/phoenix", "mlflow/mlflow",
            "wandb/weave", "e2b-dev/E2B", "openai/codex", "google-gemini/gemini-cli", "sst/opencode",
            "cline/cline", "RooCodeInc/Roo-Code", "continuedev/continue", "camel-ai/camel", "agno-agi/agno",
            "Chainlit/chainlit", "open-webui/open-webui", "lobehub/lobe-chat", "danny-avila/LibreChat",
            "n8n-io/n8n", "langgenius/dify", "FlowiseAI/Flowise", "comfyanonymous/ComfyUI",
            "anthropics/claude-code", "anthropics/anthropic-cookbook", "openai/openai-python",
            "microsoft/magentic-ui", "microsoft/playwright-mcp", "github/github-mcp-server",
            "cloudflare/agents", "vercel/ai", "mastra-ai/mastra", "firecrawl/firecrawl", "mendableai/firecrawl",
            "Cinnamon/kotaemon", "infiniflow/ragflow", "HKUDS/LightRAG", "microsoft/graphrag",
            "QuivrHQ/quivr", "vercel/ai-chatbot", "ItzCrazyKns/Perplexica", "danswer-ai/danswer", "onyx-dot-app/onyx",
            "TransformerOptimus/SuperAGI", "MervinPraison/PraisonAI", "OpenBMB/ChatDev", "geekan/MetaGPT",
            "Fosowl/agenticSeek", "kortix-ai/suna", "bytedance/deer-flow", "steel-dev/steel-browser",
            "microsoft/BitNet", "pipecat-ai/pipecat", "livekit/agents", "fixie-ai/ultravox",
            "openai/swarm", "strands-agents/sdk-python", "aws/bedrock-agentcore-sdk-python",
                    "anthropics/anthropic-sdk-python", "openai/openai-node", "Portkey-AI/gateway", "Helicone/helicone", "gradio-app/gradio", "langchain-ai/langchainjs", "microsoft/mcp",
        ],
    },
    "ml-core": {
        "label": "Core ML frameworks, compilers, kernels",
        "topics": ["deep-learning", "machine-learning", "pytorch", "jax", "compiler", "gpu", "kernels",
                   "tensor", "autodiff", "mlir", "onnx"],
        "keywords": ["tensor", "autograd", "kernel", "compiler", "mlir", "gpu", "cuda", "metal", "rocm"],
        "seeds": [
            "pytorch/pytorch", "jax-ml/jax", "huggingface/diffusers", "keras-team/keras", "onnx/onnx",
            "microsoft/onnxruntime", "openvinotoolkit/openvino", "apple/coremltools", "google/flax",
            "tinygrad/tinygrad", "karpathy/nanochat", "KellerJordan/modded-nanogpt", "NVIDIA/apex",
            "state-spaces/mamba", "pytorch/executorch", "pytorch/xla", "openxla/xla", "iree-org/iree",
            "apache/tvm", "llvm/torch-mlir", "NVIDIA/cccl", "NVIDIA/cuda-python", "cupy/cupy",
            "rapidsai/cudf", "rapidsai/cuml", "google-ai-edge/LiteRT", "google-ai-edge/mediapipe",
            "ml-explore/mlx-examples", "huggingface/kernels", "pytorch/helion", "linkedin/Liger-Kernel",
            "NVIDIA/TransformerEngine", "NVIDIA/nccl", "facebookresearch/faiss", "qdrant/qdrant",
            "milvus-io/milvus", "chroma-core/chroma", "lancedb/lancedb", "weaviate/weaviate",
            "scikit-learn/scikit-learn", "numpy/numpy", "pola-rs/polars", "dmlc/xgboost", "microsoft/LightGBM",
            "optuna/optuna", "huggingface/safetensors", "ggml-org/ggml", "triton-lang/triton",
            "openai/whisper", "huggingface/parler-tts", "modular/modular", "zml/zml",
                    "google/sentencepiece", "pytorch/vision", "pytorch/audio", "pytorch/data", "huggingface/tokenizers",
        ],
    },
    "security-vendors": {
        "label": "Security companies' open source (Cisco, Palo Alto, CrowdStrike, Rapid7, Snyk, Trail of Bits, ...)",
        "topics": ["security", "security-tools", "vulnerability", "vulnerability-scanner", "security-scanner",
                   "devsecopts", "sast", "dast", "infosec", "cybersecurity", "cloud-security", "container-security"],
        "keywords": ["security scanner", "vulnerability", "sast", "dast", "secrets detection", "misconfiguration",
                     "cloud security", "container security", "sbom", "cve", "exploit", "detection", "threat"],
        "seeds": [
            "aquasecurity/trivy", "aquasecurity/kube-bench", "aquasecurity/tfsec", "aquasecurity/kube-hunter",
            "aquasecurity/tracee", "aquasecurity/cloudsploit", "aquasecurity/trivy-operator", "aquasecurity/trivy-action",
            "projectdiscovery/nuclei", "projectdiscovery/katana", "projectdiscovery/subfinder", "projectdiscovery/httpx",
            "projectdiscovery/naabu", "projectdiscovery/uncover", "projectdiscovery/notify", "projectdiscovery/nuclei-templates",
            "trailofbits/algo", "trailofbits/graphtage", "trailofbits/buttercup", "trailofbits/pip-audit", "trailofbits/it-depends",
            "falcosecurity/falco", "falcosecurity/falcoctl", "wazuh/wazuh", "wazuh/wazuh-dashboard",
            "snyk/cli", "snyk/driftctl", "rapid7/metasploit-framework", "rapid7/metasploit-payloads",
            "PaloAltoNetworks/pan-os-python", "PaloAltoNetworks/pan-python", "crowdstrike/falconpy", "crowdstrike/caracara",
            "cisco/mongo_fdw", "cisco/openh264", "tenable/integration-jira-cloud", "okta/okta-sdk-python",
            "OWASP/Nettacker", "OWASP/threat-dragon", "OWASP/wrongsecrets", "Juniper/contrail-controller",
        ],
    },
    "crypto-pq": {
        "label": "Cryptography, TLS/SSH, post-quantum",
        "topics": ["cryptography", "post-quantum-cryptography", "post-quantum", "pqc", "tls", "ssh", "openssl", "kyber", "ml-kem",
                   "dilithium", "ml-dsa", "lattice-cryptography", "crypto-library", "encryption"],
        "keywords": ["post-quantum", "post quantum", "ml-kem", "kyber", "dilithium", "ml-dsa", "sphincs", "falcon", "lattice",
                     "tls", "ssh", "cryptograph", "cipher", "hybrid key exchange", "kem", "x25519", "aes"],
        "seeds": [
            "openssl/openssl", "openssh/openssh-portable", "open-quantum-safe/liboqs", "open-quantum-safe/oqs-provider",
            "open-quantum-safe/liboqs-python", "PQClean/PQClean", "pq-code-package/mlkem-native", "pq-code-package/mldsa-native",
            "cloudflare/circl", "RustCrypto/KEMs", "RustCrypto/signatures", "aws/aws-lc", "aws/aws-lc-rs", "randombit/botan",
            "wolfSSL/wolfssl", "rustls/rustls", "pyca/cryptography", "jedisct1/libsodium", "google/tink", "FiloSottile/age",
            "FiloSottile/mkcert", "C2SP/CCTV", "signalapp/libsignal", "Mbed-TLS/mbedtls", "libtom/libtomcrypt", "briansmith/ring",
            "pq-crystals/kyber", "pq-crystals/dilithium", "sphincs/sphincsplus", "openssh/openssh-portable", "arkworks-rs/algebra",
            "zkcrypto/bls12_381", "dalek-cryptography/curve25519-dalek", "str4d/rage", "quic-go/quic-go", "cloudflare/quiche",
            "ClusterLabs/pacemaker", "keepassxreboot/keepassxc", "bitwarden/clients", "matrix-org/vodozemac", "WireGuard/wireguard-go",
            "Kicksecure/security-misc", "google/wycheproof", "smallstep/certificates", "letsencrypt/boulder", "cfssl", "hashicorp/vault",
        ],
    },
    "frontier": {
        "label": "Cutting-edge / futuristic (robotics, world models, bio, quantum, speech, 3D, privacy)",
        "topics": ["robotics", "embodied-ai", "world-model", "video-generation", "protein-structure",
                   "quantum-computing", "text-to-speech", "speech-recognition", "gaussian-splatting",
                   "nerf", "homomorphic-encryption", "zero-knowledge", "brain-computer-interface",
                   "autonomous-driving", "simulation", "multimodal", "vision-language-model"],
        "keywords": ["robot", "embodied", "world model", "video generation", "protein", "quantum",
                     "text-to-speech", "tts", "speech", "gaussian splatting", "nerf", "homomorphic",
                     "zero-knowledge", "multimodal", "vision-language", "autonomous"],
        "seeds": [
            "huggingface/lerobot", "NVIDIA/Isaac-GR00T", "google-deepmind/mujoco", "Genesis-Embodied-AI/Genesis",
            "isaac-sim/IsaacLab", "hpcaitech/Open-Sora", "Wan-Video/Wan2.2", "genmoai/mochi",
            "Qiskit/qiskit", "quantumlib/Cirq", "PennyLaneAI/pennylane", "NVIDIA/cuda-quantum",
            "aqlaboratory/openfold", "bytedance/Protenix", "chaidiscovery/chai-lab",
            "evolutionaryscale/esm", "jwohlwend/boltz", "SWivid/F5-TTS", "fishaudio/fish-speech",
            "k2-fsa/sherpa-onnx", "resemble-ai/chatterbox", "coqui-ai/TTS", "idiap/coqui-ai-TTS",
            "zama-ai/concrete-ml", "zama-ai/tfhe-rs", "graphdeco-inria/gaussian-splatting",
            "nerfstudio-project/nerfstudio", "nerfstudio-project/gsplat", "facebookresearch/sam2",
            "facebookresearch/dinov2", "facebookresearch/sam3", "black-forest-labs/flux",
            "Stability-AI/stable-audio-tools", "QwenLM/Qwen3-VL", "QwenLM/Qwen-Agent", "deepseek-ai/DeepSeek-V3",
            "openai/gpt-oss", "moonshotai/Kimi-K2", "MiniMax-AI/MiniMax-M2", "NousResearch/hermes-agent",
            "OpenBMB/MiniCPM-V", "lllyasviel/FramePack", "Tencent-Hunyuan/HunyuanVideo",
            "Lightricks/LTX-Video", "hao-ai-lab/FastVideo", "openpilot? ".strip("? "), "commaai/openpilot",
            "autowarefoundation/autoware", "carla-simulator/carla", "unitreerobotics/unitree_rl_gym",
            "Physical-Intelligence/openpi", "OpenDriveLab/UniAD", "waymo-research/waymax",
            "microsoft/VibeVoice", "nari-labs/dia", "canopyai/Orpheus-TTS", "Zyphra/Zonos",
            "kyutai-labs/moshi", "KwaiVGI/LivePortrait", "hacksider/Deep-Live-Cam",
            "openai/shap-e", "TencentARC/InstantMesh", "microsoft/TRELLIS", "Tencent-Hunyuan/Hunyuan3D-2",
            "apple/ml-fastvlm", "google-deepmind/gemma", "Alpha-VLLM/Lumina-Image-2.0",
            "OpenGVLab/InternVL", "PaddlePaddle/PaddleOCR", "opendatalab/MinerU", "microsoft/markitdown",
            "docling-project/docling", "unstructured-io/unstructured", "VikParuchuri/marker",
            "openai/whisper", "m-bain/whisperX", "SYSTRAN/faster-whisper", "openai/CLIP",
            "microsoft/BioGPT", "deepmind/alphafold", "google-deepmind/alphafold3", "pytorch/rl",
            "Farama-Foundation/Gymnasium", "DLR-RM/stable-baselines3", "google-deepmind/open_spiel",
            "OpenBB-finance/OpenBB", "microsoft/qlib", "ai4finance-foundation/FinRL",
        ],
    },
}

# Extra free-text GitHub search queries (beyond topic search) per domain. `{since}` filled at runtime.
SEARCH_QUERIES: dict[str, list[str]] = {
    "llm-inference": ["llm inference engine", "llm serving", "kv cache", "speculative decoding", "gguf"],
    "post-training": ["rlhf", "fine-tuning llm", "grpo", "post-training llm", "llm evaluation"],
    "security": ["llm security", "prompt injection", "ai red teaming", "vulnerability scanner", "sast"],
    "agents-mcp": ["mcp server", "ai agent framework", "coding agent", "llm agents", "rag framework"],
    "ml-core": ["deep learning framework", "gpu kernels", "ml compiler", "tensor library"],
    "frontier": ["robot learning", "world model", "text to speech", "protein structure", "gaussian splatting",
                 "vision language model"],
    "crypto-pq": ["post-quantum cryptography", "ml-kem kyber", "tls library", "ssh implementation", "cryptography library"],
}


def domain_seeds() -> dict[str, list[str]]:
    """Return {domain: [owner/name]} with junk entries removed and normalized."""
    out = {}
    for k, d in DOMAINS.items():
        seen, clean = set(), []
        for s in d["seeds"]:
            s = s.strip()
            if "/" not in s or " " in s or "?" in s:
                continue
            if s.lower() in seen:
                continue
            seen.add(s.lower())
            clean.append(s)
        out[k] = clean
    return out


# --------------------------------------------------------------------------------------
# Credentials & binaries
# --------------------------------------------------------------------------------------
def github_token() -> str:
    tok = os.environ.get("OSC_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    try:
        out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n",
                             capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def claude_binary() -> str:
    env = os.environ.get("OSC_CLAUDE_BIN")
    if env and Path(env).exists():
        return env
    for cand in ("claude", str(Path.home() / ".claude" / "local" / "claude")):
        try:
            r = subprocess.run([cand, "--version"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return cand
        except Exception:
            pass
    globbed = sorted(glob.glob(str(Path.home() / ".vscode/extensions/anthropic.claude-code-*-darwin-arm64/resources/native-binary/claude")),
                     key=lambda p: [int(x) if x.isdigit() else x for x in p.replace("-darwin", ".").split("/")[-4].split("-")[-1].split(".")])
    if globbed:
        return globbed[-1]
    raise RuntimeError("Claude CLI not found; set OSC_CLAUDE_BIN")
