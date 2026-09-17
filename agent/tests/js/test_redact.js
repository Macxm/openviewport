// Runs in the `lint` service: node agent/tests/js/test_redact.js
// The browser's redact() must match the agent's on every shared case.
import { readFileSync } from 'node:fs';
import { redact } from '../../viewport/web/redact.js';

const cases = JSON.parse(readFileSync(new URL('../redaction_cases.json', import.meta.url)));
let failed = 0;
for (const c of cases) {
  const got = redact(c.input);
  if (got !== c.expected) {
    failed += 1;
    console.error(`FAIL ${c.name}\n  expected: ${c.expected}\n  got:      ${got}`);
  }
}
if (failed) {
  console.error(`${failed} of ${cases.length} redaction cases failed`);
  process.exit(1);
}
console.log(`JS redact OK: ${cases.length} shared cases`);
