# biomem — portable cognitive memory for LLM conversations

A local, associative, biologically-inspired persistent memory for LLMs:
an STM/LTM two-layer architecture with a 3D latent "cognitive terrain",
diffusion, homeostasis, RBF kernel attention and sleep-style consolidation.
Pure Python 3.10+, running fully offline on macOS / Linux / Windows.

The STM/LTM, latent-terrain, RBF-attention, diffusion, homeostasis, emotional
trace, and sleep-consolidation concepts follow the published OpenTechLab
research: [Persistent Memory for Decoder-Only Transformers](https://doi.org/10.5281/zenodo.18198327)
and [Implementation of Persistent Latent Memory for Decoder Transformers](https://doi.org/10.5281/zenodo.18267378).
The concrete BioMem coefficients and local text/browser integration are
implementation-specific and are covered by this repository's tests.

## Layout

```
src/memory_module/          the package (all modules, pure Python)
  config.py                 MemoryConfig (exact constants) + leak helpers
  memory_centers.py         RBF kernel-field core (hybrid metric, compound keys, texts)
  consolidation.py          SleepConsolidator / AutomaticConsolidator (STM→LTM)
  embedder.py               TextEmbedder (sentence-transformers) + EmotionExtractor
  text_memory.py            TextMemory main API (store/recall/step/consolidate/save/load)
  terrain_3d.py             3D latent terrain (diffusion, homeostasis, splat, blur)
  projections.py            projection bundle (embedding → 64D/16D keys, values, context, 3D)
  protocol.py               CommandHandler (WS command protocol, PAM prompts)
  ws_server.py              aiohttp WebSocket server (host 127.0.0.1:8765)
  http_fallback.py          REST fallback server
  conversation_handler.py   8-step local conversation flow (retrieve→prompt→LLM→PAM→store)
  llm_client.py             async multi-provider LLM client (OpenAI/Anthropic/Gemini/Ollama)
  settings_manager.py       encrypted local application settings
  security.py               local origin checks, state machine, data_dir
  session_cache.py          retrieve→store query pairing cache (TTL)
  telemetry.py              explicitly opt-in usage telemetry
  net.py                    network helpers
  bdbm_container.py          portable .bdbm state container with legacy-format loading
  thread_store.py           SQLite thread store (AES-256-GCM)
  cluster: dashboard.py (PyQt6), cli.py, main.py, tray_icon.py, autostart.py,
  update_checker.py, cognitive_audit.py, localization.py, utils/hw_fingerprint.py
```

## Key algorithm

- **Embedding**: `paraphrase-multilingual-MiniLM-L12-v2` (384-d), normalized.
- **LTM**: 4096 centers × 64-d keys, 128-d values, 4-d emotion; Sigma-read 0.5, write 0.15;
  leak ≈ 2.66e-5 (half-life ~1 year); new-center threshold 0.78.
- **STM**: 512 centers × 16-d keys; sigma-read 0.4 / write 0.2; leak 3.5e-3 (days–weeks);
  new-center threshold 0.5.
- **Hybrid metric**: cosine + Minkowski(p=0.5) weighted 0.7/0.3, candidate pre-selection 64.
- **Write strength** (ω = 3·intensity·σ(2·novelty + 0.3·surprise + 0.3·salience − 1)).
- **Recall** via MemoryAttention: π = softmax(log(h) − d²/2σ²), reads r_V/r_E via einsum.
- **Terrain**: 48³ grid, H + 4-channel E; splat Gaussian (σ=0.1), diffusion via 6-neighbor
  Laplacian, leak/homeostasis, blur, STM→LTM pour (ξ_h=0.005, ξ_e=0.003, blur σ=2.0).
- **Fatigue / sleep**: F ← (1−0.007)F + 0.1·Σω; sleep at F > 2.5; consolidation κ=0.8,
  top-128 STM, normalization h=log1p(h), V=V/(1+‖V‖/2), e=tanh(e), F ← 0.2·F;
  merge τ=0.95, prune 0.001/300 steps.
- **Protocol**: WS JSON commands; the conversation flow injects the STPAM/MIDPAM/ENDPAM
  memory-summary prompt and |TITLE| thread-title convention.

## Complete feature set

biomem always runs with every feature available:

- No expiry, lockouts, or feature gates; telemetry is off unless explicitly enabled
- Local/native and browser-extension origins allowed, public-page origins
  denied at the transport boundary, every protocol command open
- Portable `.bdbm` backups and exports
- No feature-mode environment switch exists
- Memory data and memory-engine computation stay on the local machine.

## Install & run

```bash
cd src
python -m venv .venv && . .venv/bin/activate
pip install -e .            # installs torch/transformers/... + biomem-server entry
biomem-server               # GUI dashboard + WS server on 127.0.0.1:8765
biomem-server --no-gui --debug # headless mode
biomem interactive          # CLI
```

### Cross-platform notes
- **macOS (arm64, x86_64), Linux (arm64, x86_64), Windows (x64)**: pure-Python package;
  torch/sentence-transformers wheels are available for all these platforms (CPU or GPU).
- Platform-specific code (autostart, tray, fingerprint, paths) is isolated behind
  `sys.platform` branches.
- WS server is asyncio/aiohttp; GUI is PyQt6 (optional `pip install .[gui]`); all math is plain torch — all math is plain torch.

## Benchmarking

The local performance suite covers retention, top-k retrieval accuracy,
concurrent access, persistence, and bounded latency. Run it with
`pytest tests/test_memory_performance.py`.
