# Configuration

Application settings use a YAML file plus `SMFA_`-prefixed environment variables. Command-line overrides take precedence over environment variables, which take precedence over the file and built-in defaults.

Safe example:

```yaml
vault_path: ~/Documents/SMFA-Vault
state_db_path: ~/Documents/SMFA-Vault/.social-media-favorites-archiver/archive.db
cache_path: ~/.cache/social-media-favorites-archiver
cache_quota_bytes: 21474836480
browser_cdp_url: http://127.0.0.1:9222
browser_profile_path: ~/.local/share/social-media-favorites-archiver/browser
enabled_platforms: [bilibili, xiaohongshu, douyin]
concurrency: 2
retries: 3
early_stop_threshold: 20
cleanup_policy: after-verified
asr_backend: auto
asr_model: funasr-paraformer-zh
ocr_backend: rapidocr
terminology_dictionary: ~/.config/social-media-favorites-archiver/terms.txt
enrichment_enabled: false
```

Every YAML key has an uppercase `SMFA_` environment equivalent, such as `SMFA_VAULT_PATH`, `SMFA_CONCURRENCY`, and `SMFA_ASR_BACKEND`. Do not place cookies, browser storage, passwords, or platform tokens in this file.

Optional OpenAI-compatible enrichment reads `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` only when enrichment runs. These variables are not persisted in application settings, and `smfa doctor` reports only whether each one is present. Local collection, ASR, OCR, and Markdown output continue to work when enrichment is disabled or those variables are absent.

## Local OCR compatibility evidence

The macOS default is RapidOCR backed by ONNX Runtime. PaddleOCR is intentionally not a required 1.0 dependency. Install the local backend with `uv sync --extra ocr`, then verify it without exposing content or credentials:

```bash
uv run smfa doctor --json
uv run pytest tests/integration/test_rapidocr.py -q -m heavyweight
```

On 2026-08-08, the generated high-contrast text check passed on Apple Silicon (`arm64`, macOS 26.6.1) with `rapidocr-onnxruntime` 1.4.4, `onnxruntime` 1.28.0, and `opencv-python` 5.0.0.93. The doctor reported `RapidOCR is available`. This proves that local runtime combination, not universal recognition quality; rotated, low-contrast, Chinese, and platform-sourced images remain explicit regression and live-validation cases.
