import { test } from "node:test";
import assert from "node:assert/strict";

import { extractMermaidBlocks, validateDiagram } from "./mermaid-check.mjs";

test("extractMermaidBlocks finds blocks with 1-based opening-fence line numbers", () => {
  const markdown = ["intro", "```mermaid", "flowchart TD", "  A --> B", "```", "outro"].join("\n");
  const blocks = extractMermaidBlocks(markdown);
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].startLine, 2);
  assert.equal(blocks[0].code, "flowchart TD\n  A --> B");
});

test("extractMermaidBlocks ignores non-mermaid fences and de-indents nested blocks", () => {
  const markdown = ["```python", "x = 1", "```", "  ```mermaid", "  graph LR", "  X --> Y", "  ```"].join(
    "\n",
  );
  const blocks = extractMermaidBlocks(markdown);
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].code, "graph LR\nX --> Y");
});

test("extractMermaidBlocks rejects an unterminated block", () => {
  assert.throws(() => extractMermaidBlocks("```mermaid\ngraph TD\nA-->B"), /Unterminated/);
});

// Proves the jsdom + DOMPurify environment is wired up: a real, valid diagram
// must parse. If the DOM setup were wrong, mermaid.parse would reject every
// diagram and this test would fail — catching the silent-misconfiguration trap.
test("validateDiagram accepts a valid flowchart", async () => {
  const result = await validateDiagram("flowchart TD\n  A[Start] --> B[End]");
  assert.deepEqual(result, { valid: true });
});

test("validateDiagram accepts a valid sequence diagram", async () => {
  const result = await validateDiagram("sequenceDiagram\n  Alice->>Bob: Hello");
  assert.equal(result.valid, true);
});

test("validateDiagram rejects a malformed diagram", async () => {
  const result = await validateDiagram("flowchart TD\n  A --> ");
  assert.equal(result.valid, false);
  assert.ok(result.error && result.error.length > 0);
});

test("validateDiagram rejects an unknown diagram type", async () => {
  const result = await validateDiagram("notADiagramType\n  garbage here");
  assert.equal(result.valid, false);
});
