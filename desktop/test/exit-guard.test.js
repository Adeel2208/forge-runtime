/**
 * The rule that decides whether a server's exit is worth telling the user
 * about.
 *
 * This existed as `if (server && ...)` and produced a reported bug: opening a
 * second folder stopped the first server, whose exit event arrived after
 * `server` already pointed at the replacement - so the guard saw a truthy
 * value, called a deliberate shutdown a crash, and showed the *old* server's
 * startup log as the evidence. The window then sat behind a modal dialog.
 *
 * Run with: node --test desktop/test
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");

// main.js exits early unless it is running under Electron, so the guard is
// re-declared here from the same source rather than imported.
const source = require("node:fs").readFileSync(
  require("node:path").join(__dirname, "..", "main.js"),
  "utf8"
);

const body = source.slice(
  source.indexOf("function shouldReportExit("),
  source.indexOf("}", source.indexOf("return current === exited")) + 1
);
// eslint-disable-next-line no-new-func
const shouldReportExit = new Function(`${body}; return shouldReportExit;`)();

const A = { pid: 1 };
const B = { pid: 2 };

test("a crash of the current server is reported", () => {
  assert.strictEqual(shouldReportExit(A, A, true), true);
});

test("a superseded server's shutdown is not reported", () => {
  // The reported bug: `server` is already B when A's exit event arrives.
  assert.strictEqual(shouldReportExit(B, A, true), false);
});

test("a deliberate stop leaves no current server, so nothing is reported", () => {
  assert.strictEqual(shouldReportExit(null, A, true), false);
});

test("nothing is reported once the window is gone", () => {
  assert.strictEqual(shouldReportExit(A, A, false), false);
});

test("truthiness alone would have reported the superseded case", () => {
  // Guards the regression rather than the fix: the old condition is written
  // out so that reverting to it fails here.
  const old = (current, _exited, alive) => Boolean(current) && alive;
  assert.strictEqual(old(B, A, true), true, "the old rule reported it");
  assert.strictEqual(shouldReportExit(B, A, true), false, "the new rule does not");
});
