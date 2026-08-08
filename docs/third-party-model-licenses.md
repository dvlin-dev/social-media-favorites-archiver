# Third-party runtimes and model licenses

The source code in this repository is MIT-licensed. That license does not relicense third-party runtimes, native libraries, or model weights. Model weights are not redistributed in the Python package, Skill bundle, GitHub release, or test fixtures; optional backends download or use models selected by the operator at runtime.

## Runtime code reviewed on 2026-08-08

| Component | Role | Upstream code license | Primary source |
|---|---|---|---|
| FunASR | Local ASR runtime | MIT | [FunASR LICENSE](https://github.com/modelscope/FunASR/blob/main/LICENSE) |
| mlx-whisper / MLX examples | Apple Silicon Whisper runtime/examples | MIT | [MLX examples LICENSE](https://github.com/ml-explore/mlx-examples/blob/main/LICENSE) |
| whisper.cpp | Local Whisper runtime | MIT | [whisper.cpp LICENSE](https://github.com/ggml-org/whisper.cpp/blob/master/LICENSE) |
| faster-whisper | Optional Linux/NVIDIA ASR runtime | MIT | [faster-whisper LICENSE](https://github.com/SYSTRAN/faster-whisper/blob/master/LICENSE) |
| RapidOCR | Local OCR runtime and tooling | Apache-2.0 | [RapidOCR LICENSE](https://github.com/RapidAI/RapidOCR/blob/main/LICENSE) |
| ONNX Runtime | RapidOCR inference runtime | MIT | [ONNX Runtime LICENSE](https://github.com/microsoft/onnxruntime/blob/main/LICENSE) |

These entries describe upstream runtime repositories, not every downloadable model artifact.

## Model-weight rule

The operator must review the exact model card and license for every configured model identifier before downloading or using it. In particular:

- a FunASR runtime license does not automatically grant the same terms for every ModelScope/Hugging Face model;
- an MLX or faster-whisper conversion can carry metadata and restrictions inherited from its source Whisper checkpoint or converter;
- RapidOCR detection, classification, and recognition weights can have notices separate from the Apache-2.0 runtime repository;
- a local path supplied to whisper.cpp remains governed by the source of that checkpoint.

Do not redistribute weights through this project. Record the chosen model identifier, upstream URL, version or revision, and license in private deployment documentation. If a model card is missing, ambiguous, or incompatible with the intended use, do not install that model. The automated dependency inventory covers Python distributions only; it cannot approve model weights.
