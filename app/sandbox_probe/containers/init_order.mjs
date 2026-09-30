import { spawn } from 'node:child_process';
import { readdirSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const root = resolve(process.argv[2] ?? 'dist');
const CONCURRENCY = 4;
const TIMEOUT_MS = 15000;
// 순환 참조 초기화 순서 오류만 증거로 본다. 환경(DB 연결 등) 때문에 죽는 파일은 오탐이라 제외한다.
const INIT_ERROR = /ReferenceError|before initialization|Class extends value/;

function walk(dir) {
  const files = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== 'node_modules') files.push(...walk(path));
    } else if (/\.m?js$/.test(entry.name)) {
      files.push(path);
    }
  }
  return files.sort();
}

function importOne(file) {
  return new Promise((done) => {
    const code = `await import(${JSON.stringify(pathToFileURL(file).href)})`;
    const child = spawn(process.execPath, ['--input-type=module', '-e', code], {
      stdio: ['ignore', 'ignore', 'pipe'],
    });
    let stderr = '';
    child.stderr.on('data', (chunk) => {
      if (stderr.length < 4096) stderr += chunk;
    });
    const timer = setTimeout(() => child.kill('SIGKILL'), TIMEOUT_MS);
    child.on('close', (exitCode, signal) => {
      clearTimeout(timer);
      const failed = exitCode !== 0 && signal === null && INIT_ERROR.test(stderr);
      done(failed ? { file: relative(root, file), error: stderr.trim().slice(-1500) } : null);
    });
  });
}

const files = walk(root);
const failures = [];
let next = 0;
await Promise.all(
  Array.from({ length: CONCURRENCY }, async () => {
    while (next < files.length) {
      const result = await importOne(files[next++]);
      if (result) failures.push(result);
    }
  }),
);
failures.sort((a, b) => a.file.localeCompare(b.file));
console.log(JSON.stringify({ checked: files.length, failures }));
