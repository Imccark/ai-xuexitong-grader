# Local PP-DocLayoutV3 runtime

This directory is reserved for the frozen local layout model used by the
grading pipeline. Model weights are intentionally not committed to Git.

## Expected layout

```text
models/
  document_layout/
    PP-DocLayoutV3/       # exported PaddleOCR/PaddleX inference or ONNX model directory
  ocr/
    PP-OCRv5_mobile_det/  # local question-number detector
    PP-OCRv5_mobile_rec/  # local question-number recognizer
```

The production configuration is in `app/configs/agent_pipeline.json`. Automatic
model downloads are disabled. The current main path uses the official default
PP-DocLayoutV3 ONNX export plus PP-OCRv5 mobile OCR and deterministic region
merging. Until all three local directories are present, the pipeline records
`local_layout_unavailable_online_fallback` and continues through the existing
online `PageObserver`.

## Deployment contract

1. Install the `layout` dependency extra with `uv sync --extra layout`.
2. Copy the official `PP-DocLayoutV3_onnx` export into
   `models/document_layout/PP-DocLayoutV3`.
3. Copy the official PP-OCRv5 mobile detector and recognizer exports into the
   two configured `models/ocr` directories.
4. Run the local layout tests and a frozen-page benchmark.
5. Keep the online observer and question locator enabled as fallbacks.

The downloaded weights are intentionally ignored by Git. If a fine-tuned model
is introduced later, record its dataset hash, hidden-test report, export tool
versions, and model SHA-256 before replacing the default export.

The local detector is answer-blind. It may see the allowed question IDs and
aliases for deterministic label matching, but it must never receive answer
content, candidate verdicts, or API credentials.
