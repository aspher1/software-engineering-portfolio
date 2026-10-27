import { existsSync, readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((item) => {
    if (item.name.startsWith('.git') || item.name === 'node_modules') return [];
    const absolute = path.join(dir, item.name);
    return item.isDirectory() ? walk(absolute) : item.name.endsWith('.md') ? [absolute] : [];
  });
}

const failures = [];
let checked = 0;
let files = 0;

for (const absolute of walk(root)) {
  files++;
  const content = readFileSync(absolute, 'utf8');
  const matches = content.matchAll(/!?\[[^\]]*\]\(([^)\s]+)(?:\s+["'][^"']*["'])?\)/g);
  for (const [, rawHref] of matches) {
    const href = rawHref.replace(/^<|>$/g, '').split('#')[0].split('?')[0];
    if (!href || /^[a-z][a-z\d+.-]*:/i.test(href) || href.startsWith('//')) continue;
    let decoded;
    try {
      decoded = decodeURIComponent(href);
    } catch {
      failures.push(path.relative(root, absolute) + ': invalid URL encoding: ' + rawHref);
      continue;
    }
    const target = path.resolve(path.dirname(absolute), decoded);
    checked++;
    if (!target.startsWith(root + path.sep) && target !== root) {
      failures.push(path.relative(root, absolute) + ': escapes repo: ' + rawHref);
    } else if (!existsSync(target)) {
      failures.push(path.relative(root, absolute) + ': missing: ' + rawHref);
    }
  }
}

console.log('Portfolio link check: ' + files + ' Markdown files, ' + checked + ' local paths');
if (failures.length) {
  for (const failure of failures) console.error(failure);
  process.exitCode = 1;
} else {
  console.log('All relative Markdown links and local screenshot assets resolve.');
}
