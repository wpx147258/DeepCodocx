# Optional Qwen3-VL-Embedding-8B native endpoint

DeepCodocx uses DashScope for both standalone embeddings and native fusion by
default, with Gitee captions and an explicit Gitee backup encoder. This adapter
is for an optional separate GPU deployment of the exact 8B
model. It is outside the Web host and Agent Runtime.

Use a machine with memory suitable for the 8B vision model and expected image
sizes. A 12 GiB laptop GPU should not be assumed to fit the default full-precision
model. Install a CUDA-compatible PyTorch wheel, then the separate dependencies:

```bash
uv venv --python 3.12 .venv-native
uv pip install --python .venv-native/bin/python -r services/qwen-native/requirements.txt
uv pip install --python .venv-native/bin/python .

export QWEN_VL_JOINT_API_KEY="your-native-service-token"
export QWEN_VL_MODEL_PATH="/path/to/Qwen3-VL-Embedding-8B"
.venv-native/bin/python -m dcx.deepdocx.semantic.native_server --port 8790
```

`QWEN_VL_MODEL_PATH` defaults to the official model ID; that may download weights.
Point it at existing local weights for an offline deployment. The model loads once
at startup. Requests accept inline images and text within **the same sample**;
they are passed together to the multimodal encoder, without averaging vectors.

The service exposes `/health` and authenticated `POST /v1/embeddings`. It accepts
1–8 samples, bounded image/text sizes, and 64–4096 output dimensions. Truncated
embeddings are normalized again. Only the configured Qwen model identity is served.
Remote image URLs, arbitrary local image paths, and caller-selected models are
rejected. Keep the default loopback bind or put it behind an authenticated HTTPS
proxy when serving another machine.

On the dcx host, configure `joint_protocol="openai"`, `joint_base_url`,
`joint_key_env="QWEN_VL_JOINT_API_KEY"`, `joint_model="Qwen3-VL-Embedding-8B"`,
and `joint_dimensions`. See the [Chinese](../../docs/multimodal-index.md) or
[English](../../docs/multimodal-index.en.md) index guide for the full setup.

The default DashScope path has been tested with a real hosted model. This optional
adapter has protocol tests with a deterministic encoder; local 8B inference still
requires validation against the GPU environment where it is deployed.
