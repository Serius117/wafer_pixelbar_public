# 安装与运行

本仓库是 wafer-pixel 的公开快照。**不包含**音频 fixture、模型、声纹数据、
API key、开发日志和本地二进制。

## 1. 环境要求

- Windows 11
- Python 3.11+
- 可选：NVIDIA GPU（本地 9B / ASR / TTS 参考 RTX 4070S）
- PixelBar 音箱（真机模式）

## 2. 安装

```powershell
git clone https://gitee.com/Serius_117/wafer_pixelbar_public.git
cd wafer_pixelbar_public

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## 3. 配置

```powershell
Copy-Item config\providers.local.example.yaml config\providers.local.yaml
```

- 本地推理默认值：`config/inference.yaml`
- 在线 LLM / TTS：编辑 `config/providers.local.yaml`
- 真实 key：写入仓库根目录 `.env`（gitignored）

`config/projects.yaml` 里的 `./workspace` / `./music` 是示例路径，使用前请按本机修改。

## 4. 模型与推理服务

本仓库不附带模型或 `llama-server` 二进制。请自行准备：

- llama.cpp 的 `llama-server`
- Qwen3-ASR / 本地 LLM 模型
- GPT-SoVITS / CosyVoice / MiniMax（按所选 TTS 方案）

把 `llama-server` 放到 PATH，或在 `config/inference.yaml` 里填写绝对路径；
模型路径同样按本机修改。

## 5. 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

公开快照不包含音频 fixture，依赖这些素材的音频回归测试不会随仓库分发。

## 6. OLV 语音页（可选，真机语音需要）

公开快照不包含 OLV 上游（第三方项目，`olv/upstream/` 已排除），
但**包含全部定制补丁**（`olv/patches/*.patch`）。语音页按以下步骤搭建：

```powershell
git clone https://github.com/Open-LLM-VTuber/Open-LLM-VTuber.git `
    olv/upstream/Open-LLM-VTuber
cd olv/upstream/Open-LLM-VTuber
git checkout 992309c
git apply ../../patches/p1_gate_hook_v2.patch
git apply ../../patches/p2_wafer_agent.patch
git apply ../../patches/p6_wafer_servers.patch
git apply ../../patches/p8_ws_send_lock.patch
git apply ../../patches/p11_tts_streaming.patch
git apply ../../patches/p12_state_after_speech.patch
git apply ../../patches/p13_proactive_announce.patch
git apply ../../patches/p14_tts_timing.patch
git apply ../../patches/p15_model_switch.patch
cd ../../..
```

再按 OLV 上游文档创建其独立 venv（Python 3.10，`olv/upstream/
Open-LLM-VTuber/.venv`）并安装依赖；然后运行
`python scripts/setup_olv.py` 生成 `conf.yaml`。
不带 OLV 时，`dev_stack.py start --no-olv` 可运行 hub/bridge/render。

## 7. 启动

```powershell
# 零硬件：hub + render console
python -m core.bus --port 8765
python -m render --port 8765 --console 8790

# 真机：推理三件套 + hub/bridge/render/OLV
.\.venv\Scripts\python.exe scripts\dev_stack.py start --strict-wake --render-debug
```

OLV 音频页：<http://127.0.0.1:12393/>
LED console：<http://127.0.0.1:8790/>

## 8. 隐私

本地 VAD + CAM++ 门禁 + 本地 ASR 是默认路径；在线模式只发送已放行的文本，不发送原始音频。
