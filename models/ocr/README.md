# Local question-number OCR runtime

The grading pipeline uses the official PP-OCRv5 mobile detector and recognizer
only on the left strip of each normalized page. It extracts manifest question
identifiers for deterministic layout merging; it is not the source of the
student-answer transcription used for grading.

Expected local directories:

```text
models/ocr/PP-OCRv5_mobile_det/
models/ocr/PP-OCRv5_mobile_rec/
```

Weights are intentionally ignored by Git. Missing or incompatible weights
cause the local layout gate to abstain and the existing online page observer to
run instead.
