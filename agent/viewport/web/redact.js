// Remove credentials from text before the wall shows, stores or reports it.
// go2rtc puts the camera's source URL, password included, into the error messages it
// sends the player, so an unfiltered error would draw the NVR password on the TV.
// Mirrors agent/viewport/credentials.py; agent/tests/redaction_cases.json keeps them equal.

const MASK = '***';
const KEYS = '(?:password|passwd|pwd|pass|token|secret|api_key|apikey)';

const PATTERNS = [
  // scheme://user:password@host — greedy to the last "@" before the path.
  [new RegExp(String.raw`([a-z][a-z0-9+.\-]*://)[^/\s"'<>]*@`, 'gi'), `$1${MASK}@`],
  // ?password=... &token=...
  [new RegExp(String.raw`(${KEYS}=)[^&\s"'\\#<>]*`, 'gi'), `$1${MASK}`],
  // The same two, percent-encoded inside another URL.
  [new RegExp(String.raw`(%3A%2F%2F)(?:(?!%2F)[^\s&"'/])*%40`, 'gi'), `$1${MASK}%40`],
  [new RegExp(String.raw`(${KEYS}%3D)(?:(?!%26)[^\s&"'\\])*`, 'gi'), `$1${MASK}`],
];

export function redact(text) {
  if (!text) return text || '';
  let out = String(text);
  for (const [pattern, replacement] of PATTERNS) out = out.replace(pattern, replacement);
  return out;
}
