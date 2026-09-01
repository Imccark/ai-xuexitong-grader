const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const ROOT = __dirname;
const UI_DIR = path.join(ROOT, "review_ui");
const VENDOR_DIR = path.join(UI_DIR, "vendor");
const KATEX_CSS_PATH = path.join(VENDOR_DIR, "css", "katex.min.css");
const MARKED_JS_PATH = path.join(VENDOR_DIR, "js", "marked.min.js");
const DOMPURIFY_JS_PATH = path.join(VENDOR_DIR, "js", "dompurify.min.js");
const KATEX_JS_PATH = path.join(VENDOR_DIR, "js", "katex.min.js");
const KATEX_AUTO_RENDER_JS_PATH = path.join(VENDOR_DIR, "js", "katex-auto-render.min.js");

const VIEWPORT_WIDTH = 1600;
const DEFAULT_VIEWPORT_HEIGHT = 900;
const DEVICE_SCALE_FACTOR = 2;
const CARD_WIDTH = 1200;

function parseArgs(argv) {
  const args = { output: "" };
  for (let index = 2; index < argv.length; index += 1) {
    const value = String(argv[index] || "");
    if (value === "--output") {
      args.output = String(argv[index + 1] || "");
      index += 1;
    }
  }
  return args;
}

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
    process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    process.stdin.on("error", reject);
  });
}

function escapeRegExp(str) {
  return String(str).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function normalizeToMarkdown(text) {
  let source = String(text || "")
    .replace(/\r\n?/g, "\n")
    .trim();

  source = source.replace(/^[=\-*_]{6,}\s*$/gm, "");

  const fields = ["姓名/学号", "整体情况"];
  for (const name of fields) {
    const pattern = new RegExp(`^\\s*${escapeRegExp(name)}\\s*[：:]\\s*(.*)$`, "gm");
    source = source.replace(pattern, `**${name}：**$1`);
  }

  const sections = ["错误细节", "证明题审查", "改进建议"];
  for (const name of sections) {
    const pattern = new RegExp(`^\\s*${escapeRegExp(name)}\\s*[：:]*\\s*$`, "gm");
    source = source.replace(pattern, `\n## ${name}\n`);
  }

  source = source.replace(/\n{3,}/g, "\n\n").trim();
  return source;
}

function normalizeMatrixLatex(text) {
  return String(text).replace(
    /\\begin\{(matrix|pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix|smallmatrix)\}([\s\S]*?)\\end\{\1\}/g,
    (_, env, body) => {
      const fixed = body
        .split(/\\\\/g)
        .map((row) => {
          const trimmed = row.trim();
          if (!trimmed || trimmed.includes("&")) {
            return trimmed;
          }
          const parts = trimmed.split(/\s+/).filter(Boolean);
          if (parts.length < 2) {
            return trimmed;
          }
          return parts.join(" & ");
        })
        .join(" \\\\ ");
      return `\\begin{${env}}${fixed}\\end{${env}}`;
    },
  );
}

function normalizeLatexExpression(expr) {
  return String(expr || "")
    .replace(/[＼﹨∖]/g, "\\")
    .replace(/(^|[\s{[(,;:=<>+\-*])[/／]([A-Za-z]+)\b/g, (_match, prefix, command) => `${prefix}\\${command}`);
}

function normalizeMathCommandSyntax(source) {
  return String(source || "")
    .replace(/\\\[([\s\S]+?)\\\]/g, (_match, expr) => `\\[${normalizeLatexExpression(expr)}\\]`)
    .replace(/\\\(([\s\S]+?)\\\)/g, (_match, expr) => `\\(${normalizeLatexExpression(expr)}\\)`)
    .replace(/\$\$([\s\S]+?)\$\$/g, (_match, expr) => `$$${normalizeLatexExpression(expr)}$$`)
    .replace(/\$([^\n$]+?)\$/g, (_match, expr) => `$${normalizeLatexExpression(expr)}$`);
}

function fileUrl(filePath) {
  const normalized = path.resolve(filePath).split(path.sep).join("/");
  return `file://${normalized}`;
}

function rewriteKatexCssAssetUrls(cssText) {
  return String(cssText || "").replace(/url\((['"]?)(\.\.\/fonts\/[^'")]+)\1\)/g, (_match, _quote, relative) => {
    const fontName = relative.replace(/^\.\.\/fonts\//, "");
    const fontPath = path.join(VENDOR_DIR, "fonts", fontName);
    try {
      const fontBuffer = fs.readFileSync(fontPath);
      const extension = path.extname(fontPath).toLowerCase();
      const mimeType =
        extension === ".woff2"
          ? "font/woff2"
          : extension === ".woff"
            ? "font/woff"
            : extension === ".ttf"
              ? "font/ttf"
              : "application/octet-stream";
      return `url("data:${mimeType};base64,${fontBuffer.toString("base64")}")`;
    } catch (_error) {
      return `url("${fileUrl(fontPath)}")`;
    }
  });
}

function readAsset(assetPath, label) {
  try {
    return fs.readFileSync(assetPath, "utf8");
  } catch (error) {
    throw new Error(`读取 ${label} 失败: ${error.message}`);
  }
}

function buildHtml(sourceText) {
  const katexCss = rewriteKatexCssAssetUrls(readAsset(KATEX_CSS_PATH, "KaTeX CSS"));
  const markedJs = readAsset(MARKED_JS_PATH, "marked");
  const dompurifyJs = readAsset(DOMPURIFY_JS_PATH, "DOMPurify");
  const katexJs = readAsset(KATEX_JS_PATH, "KaTeX");
  const katexAutoRenderJs = readAsset(KATEX_AUTO_RENDER_JS_PATH, "KaTeX auto-render");
  const normalized = normalizeMathCommandSyntax(normalizeMatrixLatex(normalizeToMarkdown(sourceText)));
  const sourceJson = JSON.stringify(normalized);

  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <style>
    ${katexCss}

    :root {
      --bg: #f6f8fb;
      --card: #ffffff;
      --text: #1f2937;
      --title: #111827;
      --muted: #4b5563;
      --border: #e5e7eb;
      --shadow: 0 12px 36px rgba(0, 0, 0, 0.08);
      --accent: #dbeafe;
    }

    * {
      box-sizing: border-box;
    }

    html, body {
      margin: 0;
      padding: 0;
      background: var(--bg);
    }

    body {
      padding: 32px;
      color: var(--text);
      font-family:
        "Microsoft YaHei",
        "PingFang SC",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "Segoe UI",
        system-ui,
        sans-serif;
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }

    #card {
      width: ${CARD_WIDTH}px;
      margin: 0 auto;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 20px;
      box-shadow: var(--shadow);
      padding: 36px 42px;
      overflow: visible;
    }

    #content {
      color: var(--text);
    }

    h1, h2, h3, h4, h5, h6, p, ol, ul, li, blockquote {
      margin: 0;
      padding: 0;
    }

    p, li, blockquote {
      font-size: 26px;
      line-height: 1.9;
      color: var(--text);
      white-space: normal;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    p {
      margin-bottom: 12px;
    }

    h2 {
      font-size: 31px;
      line-height: 1.4;
      color: var(--title);
      margin-top: 30px;
      margin-bottom: 14px;
      padding-left: 14px;
      border-left: 6px solid var(--accent);
    }

    h3 {
      font-size: 28px;
      line-height: 1.4;
      color: var(--title);
      margin-top: 24px;
      margin-bottom: 10px;
    }

    ol, ul {
      margin-top: 8px;
      padding-left: 34px;
    }

    li {
      margin: 12px 0;
    }

    strong {
      color: var(--title);
      font-weight: 700;
    }

    code, pre {
      font-family:
        "Cascadia Code",
        "Consolas",
        "JetBrains Mono",
        monospace;
    }

    pre {
      font-size: 22px;
      line-height: 1.7;
      padding: 16px 18px;
      background: #f8fafc;
      border: 1px solid #e5e7eb;
      border-radius: 12px;
      overflow: auto;
      margin: 14px 0;
      white-space: pre-wrap;
      word-break: break-word;
    }

    code {
      font-size: 0.92em;
    }

    blockquote {
      margin: 14px 0;
      padding: 10px 14px;
      border-left: 4px solid #cbd5e1;
      background: #f8fafc;
      color: var(--muted);
    }

    hr {
      border: 0;
      border-top: 1px solid var(--border);
      margin: 24px 0;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      margin: 16px 0;
      font-size: 22px;
    }

    th, td {
      border: 1px solid var(--border);
      padding: 10px 12px;
      text-align: left;
      vertical-align: top;
    }

    th {
      background: #f8fafc;
      color: var(--title);
    }

    img {
      max-width: 100%;
      height: auto;
    }

    .katex-display {
      overflow-x: auto;
      overflow-y: hidden;
      margin: 12px 0 16px;
      padding: 2px 0;
    }

    .katex {
      font-size: 1.05em;
    }
  </style>
</head>
<body>
  <div id="card">
    <div id="content"></div>
  </div>
  <script>${markedJs}</script>
  <script>${dompurifyJs}</script>
  <script>${katexJs}</script>
  <script>${katexAutoRenderJs}</script>
  <script>
    const sourceText = ${sourceJson};
    marked.setOptions({
      breaks: true,
      gfm: true,
      headerIds: false,
      mangle: false,
    });
    const rawHtml = marked.parse(sourceText);
    const safeHtml = DOMPurify.sanitize(rawHtml);
    const contentEl = document.getElementById("content");
    contentEl.innerHTML = safeHtml;
    renderMathInElement(contentEl, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\\\[", right: "\\\\]", display: true },
        { left: "\\\\(", right: "\\\\)", display: false },
        { left: "$", right: "$", display: false },
      ],
      throwOnError: false,
      strict: "ignore",
      output: "htmlAndMathml",
      trust: false,
    });
  </script>
</body>
</html>`;
}

async function renderToFile(sourceText, outputPath) {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({
      viewport: {
        width: VIEWPORT_WIDTH,
        height: DEFAULT_VIEWPORT_HEIGHT,
      },
      deviceScaleFactor: DEVICE_SCALE_FACTOR,
    });
    const page = await context.newPage();
    const html = buildHtml(sourceText);

    await page.setViewportSize({
      width: VIEWPORT_WIDTH,
      height: DEFAULT_VIEWPORT_HEIGHT,
    });
    await page.setContent(html, { waitUntil: "domcontentloaded" });
    await page.locator("#card").waitFor({ state: "visible" });
    await page.evaluate(async () => {
      if (document.fonts && document.fonts.ready) {
        await document.fonts.ready;
      }
      await new Promise(requestAnimationFrame);
      await new Promise(requestAnimationFrame);
    });

    const card = page.locator("#card");
    const box = await card.boundingBox();
    if (box) {
      await page.setViewportSize({
        width: VIEWPORT_WIDTH,
        height: Math.max(DEFAULT_VIEWPORT_HEIGHT, Math.ceil(box.height) + 120),
      });
      await page.evaluate(async () => {
        await new Promise(requestAnimationFrame);
        await new Promise(requestAnimationFrame);
      });
    }

    await fs.promises.mkdir(path.dirname(outputPath), { recursive: true });
    await card.screenshot({
      path: outputPath,
      animations: "disabled",
      caret: "hide",
      scale: "device",
    });
    await context.close();
  } finally {
    await browser.close();
  }
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.output) {
    throw new Error("缺少 --output 参数。");
  }
  const sourceText = await readStdin();
  if (!String(sourceText || "").trim()) {
    throw new Error("导出内容为空，无法生成图片。");
  }
  await renderToFile(sourceText, path.resolve(args.output));
}

main().catch((error) => {
  const message = error && error.stack ? error.stack : String(error);
  process.stderr.write(`${message}\n`);
  process.exit(1);
});
