#!/usr/bin/env node
// Browserless Mermaid syntax check: extract ```mermaid fenced blocks from
// Markdown and validate each with the official `mermaid.parse()`. Mermaid needs
// a DOM (it wires DOMPurify against `window` at import time), so we populate
// jsdom globals *before* dynamically importing mermaid. A static `import
// mermaid` would evaluate its module init before these globals exist and make
// every parse fail.

import { readFile } from "node:fs/promises";
import { JSDOM } from "jsdom";

let mermaidModule = null;

async function loadMermaid() {
  if (mermaidModule) {
    return mermaidModule;
  }
  const dom = new JSDOM("<!DOCTYPE html><html><body></body></html>");
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  // Node defines `navigator` as a read-only global; only fill it if missing.
  if (!("navigator" in globalThis)) {
    globalThis.navigator = dom.window.navigator;
  }
  const mermaid = (await import("mermaid")).default;
  mermaid.initialize({ startOnLoad: false });
  mermaidModule = mermaid;
  return mermaid;
}

const FENCE = /^([ \t]*)```mermaid[ \t]*$/;

/**
 * Extract fenced ```mermaid blocks from Markdown source.
 *
 * @param {string} markdown Raw Markdown text.
 * @returns {{code: string, startLine: number}[]} One entry per block. `startLine`
 *   is the 1-based line of the opening fence; `code` is the block body.
 */
export function extractMermaidBlocks(markdown) {
  const lines = markdown.split("\n");
  const blocks = [];
  let current = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (current === null) {
      const match = FENCE.exec(line);
      if (match) {
        current = { startLine: i + 1, indent: match[1].length, body: [] };
      }
      continue;
    }
    if (/^[ \t]*```[ \t]*$/.test(line)) {
      blocks.push({ code: current.body.join("\n"), startLine: current.startLine });
      current = null;
      continue;
    }
    current.body.push(line.slice(current.indent));
  }
  if (current !== null) {
    throw new Error(`Unterminated \`\`\`mermaid block opened at line ${current.startLine}`);
  }
  return blocks;
}

/**
 * Validate one Mermaid diagram definition without rendering.
 *
 * @param {string} code Diagram source.
 * @returns {Promise<{valid: boolean, error?: string}>} `valid: true` for parseable
 *   diagrams; otherwise `valid: false` with the parser's error message.
 */
export async function validateDiagram(code) {
  const mermaid = await loadMermaid();
  try {
    await mermaid.parse(code);
    return { valid: true };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { valid: false, error: message };
  }
}

/**
 * Check every ```mermaid block in the given Markdown files.
 *
 * @param {string[]} files Paths to Markdown files.
 * @returns {Promise<{file: string, line: number, error: string}[]>} One entry per
 *   failing block; empty when all blocks parse.
 */
export async function checkFiles(files) {
  const failures = [];
  for (const file of files) {
    const markdown = await readFile(file, "utf8");
    const blocks = extractMermaidBlocks(markdown);
    for (const block of blocks) {
      const result = await validateDiagram(block.code);
      if (!result.valid) {
        failures.push({ file, line: block.startLine, error: result.error ?? "unknown error" });
      }
    }
  }
  return failures;
}

async function main(files) {
  if (files.length === 0) {
    process.stderr.write("usage: mermaid-check.mjs <file.md> [...]\n");
    process.exitCode = 2;
    return;
  }
  const failures = await checkFiles(files);
  let checked = 0;
  for (const file of files) {
    const markdown = await readFile(file, "utf8");
    checked += extractMermaidBlocks(markdown).length;
  }
  if (failures.length > 0) {
    for (const failure of failures) {
      process.stderr.write(`${failure.file}:${failure.line}: invalid mermaid: ${failure.error}\n`);
    }
    process.stderr.write(`\n${failures.length} of ${checked} mermaid block(s) failed to parse\n`);
    process.exitCode = 1;
    return;
  }
  process.stdout.write(`ok: ${checked} mermaid block(s) parsed in ${files.length} file(s)\n`);
}

const invokedDirectly = process.argv[1] && import.meta.url === `file://${process.argv[1]}`;
if (invokedDirectly) {
  await main(process.argv.slice(2));
}
