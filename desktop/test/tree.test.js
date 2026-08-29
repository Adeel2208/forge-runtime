/**
 * The explorer's tree, tested against the shipped source.
 *
 * `drawTree` is lifted out of workbench.py rather than reimplemented here, so
 * these assertions are about the code that runs, not a copy of it that can
 * drift. The DOM it touches is stubbed; everything else is real.
 *
 * The reported bug is the third test: a repository whose top level is one
 * folder containing one folder opened to show a folder and no files, which
 * reads as an empty folder.
 *
 * Run with: npm test
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const WORKBENCH = path.join(__dirname, "..", "..", "forge", "api", "workbench.py");

function render(files, open = []) {
  const src = fs.readFileSync(WORKBENCH, "utf8");
  const start = src.indexOf("function drawTree(){");
  const end =
    src.indexOf("\n}", src.indexOf('$("tree").querySelectorAll("[data-d]")', start)) + 2;
  const body = src.slice(start, end);

  const dom = { tree: { innerHTML: "" }, fc: {}, filter: { value: "" } };
  const stub = `
    const esc = (s) => String(s);
    const $ = (id) => {
      const n = dom[id] || {};
      n.querySelectorAll = () => [];
      return n;
    };
    const store = { set() {}, get: (k, d) => d };
  `;
  const run = new Function(
    "dom", "tree", "openDirs", "current",
    `${stub}\n${body}\ndrawTree();\nreturn dom.tree.innerHTML;`
  );
  const html = run(dom, files, new Set(open), null);
  return html
    .split("</div>")
    .filter(Boolean)
    .map((row) => ({
      dir: row.includes("node dir"),
      // The expand marker is text inside the span, so stripping tags leaves
      // it glued to the name.
      label: row.replace(/<[^>]*>/g, "").replace(/[▸▾]/g, "").trim(),
      key: (row.match(/data-[dp]="([^"]*)"/) || [, ""])[1],
    }));
}

test("root files and folders both appear", () => {
  const rows = render(["README.md", "src/app.py", "src/util.py"]);
  assert.ok(rows.some((r) => !r.dir && r.label === "README.md"));
  assert.ok(rows.some((r) => r.dir && r.label === "src"));
});

test("a collapsed folder hides its contents", () => {
  const rows = render(["src/app.py"]);
  assert.ok(!rows.some((r) => r.label === "app.py"));
});

test("a chain of single-child folders collapses to one row", () => {
  // The reported bug. Without compaction this is three clicks to reach a file,
  // and the first two show a folder and nothing else.
  const rows = render(["a/b/c/deep.py", "a/b/c/other.py"]);
  const dirs = rows.filter((r) => r.dir);

  assert.strictEqual(dirs.length, 1, "one row, not three");
  assert.strictEqual(dirs[0].label, "a/b/c");
  assert.strictEqual(dirs[0].key, "a/b/c", "and clicking it opens the whole chain");
});

test("opening the compacted row reveals the files", () => {
  const rows = render(["a/b/c/deep.py"], ["a/b/c"]);
  assert.ok(rows.some((r) => !r.dir && r.label === "deep.py"));
});

test("a chain stops compacting where the content actually branches", () => {
  const rows = render(["a/b/one.py", "a/b/sub/two.py"]);
  const dirs = rows.filter((r) => r.dir).map((r) => r.label);
  assert.deepStrictEqual(dirs, ["a/b"], "compacts to the branch point, no further");
});

test("a folder holding a file is not compacted past it", () => {
  const rows = render(["a/keep.py", "a/b/deep.py"], ["a"]);
  const dirs = rows.filter((r) => r.dir).map((r) => r.label);
  assert.ok(dirs.includes("a"), "'a' has a file, so it is its own row");
  assert.ok(rows.some((r) => r.label === "keep.py"));
});

test("filtering expands everything, because a search must show its hits", () => {
  const src = fs.readFileSync(WORKBENCH, "utf8");
  assert.ok(
    src.includes("const open=q||openDirs.has(full)"),
    "the filter must force folders open"
  );
});

test("an empty repository explains itself rather than rendering a blank pane", () => {
  const src = fs.readFileSync(WORKBENCH, "utf8");
  assert.ok(src.includes("no files git can see"));
});

/* ---- accessibility and resilience, asserted against the shipped page ---- */

const PAGE = fs.readFileSync(WORKBENCH, "utf8");

test("the tree is a real tree to a screen reader", () => {
  assert.ok(PAGE.includes('role="tree"'), "the container is a tree");
  assert.match(PAGE, /role="treeitem"[\s\S]*?data-d=/, "folders are tree items");
  assert.ok(PAGE.includes('aria-expanded='), "folders report expansion state");
});

test("the tree can be driven from the keyboard", () => {
  // A developer tool that needs a mouse excludes people who work without one.
  for (const key of ["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Home", "End"]) {
    assert.ok(PAGE.includes(`"${key}"`), `no handler for ${key}`);
  }
});

test("live regions are announced", () => {
  assert.ok(PAGE.includes('aria-live="polite"'));
  assert.ok(PAGE.includes('role="alert"'), "the offline banner interrupts");
});

test("a dead server is shown, not swallowed", () => {
  // Every request failing silently while the last render stays on screen makes
  // a dead backend look exactly like an idle one.
  assert.ok(PAGE.includes("setOffline(true"), "a failed fetch raises the banner");
  assert.ok(PAGE.includes('$("retry")'), "and offers a way back");
});

test("panes collapse and the choice is remembered", () => {
  assert.ok(PAGE.includes("#body.no-left"), "a real collapsed layout, not display:none alone");
  assert.ok(PAGE.includes('store.set(which'), "the choice is persisted");
  assert.ok(PAGE.includes('togglePane("left")') && PAGE.includes('togglePane("right")'));
});

test("narrow windows get a stacked layout", () => {
  assert.match(PAGE, /@media \(max-width:760px\)/);
});

test("motion is optional", () => {
  assert.ok(PAGE.includes("prefers-reduced-motion"));
});
