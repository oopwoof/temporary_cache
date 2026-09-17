#!/usr/bin/env node
/**
 * docs/*.md  →  docs/*.pdf
 *
 * 用系统里已有的 Chromium 打印 A4。不引入 marked 之类的依赖, 这里自带一个
 * 够用的 Markdown 渲染(标题/表格/列表/引用/粗体/行内代码/链接), 文档本身
 * 也只用到这些语法。
 *
 *   node scripts/build_pdf.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DOCS = path.join(ROOT, "docs");

const esc = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
/* 行内: `代码` → **粗体** → [文字](链接) */
function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, (_, c) => "<code>" + c + "</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');
}
const cells = row => row.replace(/^\||\|$/g, "").split("|").map(c => c.trim());

function md2html(src) {
  const lines = src.split("\n");
  const out = [];
  let inList = false, inTable = false;
  const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };
  const closeTable = () => { if (inTable) { out.push("</tbody></table>"); inTable = false; } };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // 表格: | a | b |  紧跟一行 |---|---|
    if (!inTable && /^\|.*\|$/.test(line.trim()) && /^\|[\s:|-]+\|$/.test((lines[i + 1] || "").trim())) {
      closeList();
      out.push("<table><thead><tr>"
        + cells(line.trim()).map(c => "<th>" + inline(c) + "</th>").join("")
        + "</tr></thead><tbody>");
      inTable = true; i++; continue;
    }
    if (inTable) {
      if (/^\|.*\|$/.test(line.trim())) {
        out.push("<tr>" + cells(line.trim()).map(c => "<td>" + inline(c) + "</td>").join("") + "</tr>");
        continue;
      }
      closeTable();
    }

    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { closeList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }

    const li = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
    if (li) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push("<li>" + inline(li[1]) + "</li>"); continue;
    }
    closeList();

    if (/^>\s?/.test(line)) { out.push("<blockquote>" + inline(line.replace(/^>\s?/, "")) + "</blockquote>"); continue; }
    if (/^```/.test(line)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      out.push("<pre><code>" + esc(buf.join("\n")) + "</code></pre>"); continue;
    }
    if (!line.trim()) continue;
    out.push("<p>" + inline(line) + "</p>");
  }
  closeList(); closeTable();
  return out.join("\n");
}

/* 打印样式。字体用系统里装了的文泉驿正黑, 保证中文不出豆腐块。 */
const CSS = `
@page { size: A4; margin: 12mm 11mm; }
*{box-sizing:border-box}
body{font-family:"WenQuanYi Zen Hei","Noto Sans CJK SC","PingFang SC",system-ui,sans-serif;
  font-size:8.5pt; line-height:1.42; color:#111; margin:0;}
h1{font-size:14pt; margin:0 0 2.5mm; padding-bottom:1.5mm; border-bottom:1.5pt solid #2a78d6}
h2{font-size:10.2pt; margin:3mm 0 1.2mm; color:#184f95}
h3{font-size:10pt; margin:4mm 0 1.5mm}
h4{font-size:9.5pt; margin:3mm 0 1mm}
p{margin:0 0 2mm}
ul{margin:0 0 2mm; padding-left:4.5mm}
li{margin:0 0 1.2mm}
table{width:100%; border-collapse:collapse; margin:1.5mm 0 2.5mm; font-size:7.5pt;
  table-layout:fixed; word-break:break-word}
th,td{border:0.5pt solid #c9c9c4; padding:1mm 1.4mm; text-align:left; vertical-align:top}
td code{white-space:normal; word-break:normal}
th{background:#eef4fc; font-weight:600; color:#184f95}
code{font-family:ui-monospace,Menlo,Consolas,monospace; font-size:7.6pt;
  background:#f1f1ee; padding:0 1mm; border-radius:2px}
pre{background:#f7f7f5; border:0.5pt solid #e1e0d9; border-radius:3px; padding:2mm;
  margin:0 0 2.5mm; overflow:hidden}
pre code{background:none; padding:0; font-size:8pt; line-height:1.4}
blockquote{margin:2mm 0; padding:1.5mm 3mm; border-left:2pt solid #c3c2b7;
  color:#52514e; background:#faf9f6}
strong{font-weight:600}
a{color:#1c5cab; text-decoration:none}
h2,h3,h4{break-after:avoid}
table,pre,blockquote{break-inside:avoid}
`;

// 跳过 *.template.md —— 它们是 build_docs.py 的输入, 不是给人读的文档
const files = fs.readdirSync(DOCS)
  .filter(f => f.endsWith(".md") && !f.endsWith(".template.md"));
const browser = await chromium.launch();
const page = await browser.newPage();
for (const f of files) {
  const md = fs.readFileSync(path.join(DOCS, f), "utf8");
  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>${esc(f)}</title><style>${CSS}</style></head><body>${md2html(md)}</body></html>`;
  await page.setContent(html, { waitUntil: "load" });
  // A4 版心 (210-26)×(297-28)mm @96dpi, 用来判断内容占了几页
  await page.setViewportSize({ width: 711, height: 1031 });
  const h = await page.evaluate(() => document.body.scrollHeight);
  const fill = (h / 1031 * 100).toFixed(0);
  const pdf = path.join(DOCS, f.replace(/\.md$/, ".pdf"));
  await page.pdf({ path: pdf, format: "A4", printBackground: true });
  // DOC_PNG=/某个目录 时额外出一张按 A4 版心截的图, 用来肉眼检查字号与排版
  if (process.env.DOC_PNG) {
    await page.screenshot({ path: path.join(process.env.DOC_PNG,
      f.replace(/\.md$/, ".png")), fullPage: true });
  }
  const pages = (fs.readFileSync(pdf).toString("latin1").match(/\/Type\s*\/Page[^s]/g) || []).length;
  console.log(`${f}  →  ${path.basename(pdf)}  (${pages} 页, 内容占版心 ${fill}%, ${(fs.statSync(pdf).size / 1024).toFixed(0)} KB)`);
}
await browser.close();
