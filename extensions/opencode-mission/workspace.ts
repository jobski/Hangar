import { execFile } from 'node:child_process';
import { constants, lstat, open, readlink } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { isAbsolute, join, relative, resolve } from 'node:path';
import { promisify } from 'node:util';

const exec = promisify(execFile);
const MAX_BYTES = 50 * 1024 * 1024;

// Binary patches can be larger than the source files.
const GIT_OPTS = ['--no-pager', '-c', 'core.quotePath=false'];

async function git(cwd: string, args: string[]): Promise<string> {
  const { stdout } = await exec('git', [...GIT_OPTS, ...args], {
    cwd,
    encoding: 'utf8',
    maxBuffer: MAX_BYTES * 4,
    timeout: 60000,
  });
  return stdout;
}

async function soft(cwd: string, args: string[]): Promise<string> {
  try {
    return await git(cwd, args);
  } catch {
    return '';
  }
}

function contained(root: string, abs: string): boolean {
  const rel = relative(root, abs);
  return rel !== '' && !rel.startsWith('..') && !isAbsolute(rel);
}

type Entry =
  | { kind: 'file'; abs: string; size: number }
  | { kind: 'link'; abs: string }
  | { kind: 'irregular' };

/** lstats every path component; a symlink anywhere in the path is reported, never followed. */
async function inspect(cwd: string, rel: string): Promise<Entry> {
  const root = resolve(cwd);
  const parts = rel.split('/').filter((p) => p !== '' && p !== '.');
  if (parts.length === 0) return { kind: 'irregular' };
  let cur = root;
  for (let i = 0; i < parts.length; i++) {
    cur = join(cur, parts[i]);
    if (!contained(root, cur)) return { kind: 'irregular' };
    let st;
    try {
      st = await lstat(cur);
    } catch (err) {
      throw new Error(`cannot stat '${rel}': ${(err as Error).message}`);
    }
    if (st.isSymbolicLink()) return { kind: 'link', abs: cur };
    if (i < parts.length - 1) {
      if (!st.isDirectory()) return { kind: 'irregular' };
    } else if (!st.isFile()) {
      return { kind: 'irregular' };
    }
  }
  return { kind: 'file', abs: cur, size: (await lstat(cur)).size };
}

async function readNoFollow(abs: string): Promise<Buffer> {
  const fh = await open(abs, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  try {
    const buf = await fh.readFile();
    if (buf.length > MAX_BYTES) throw new Error(`'${abs}' exceeds the 50MB limit`);
    return buf;
  } finally {
    await fh.close();
  }
}

function isBinary(buf: Buffer): boolean {
  return buf.subarray(0, 8192).includes(0);
}

async function untracked(cwd: string): Promise<string[]> {
  const out = await git(cwd, ['ls-files', '--others', '--exclude-standard', '-z']);
  return out.split('\0').filter(Boolean);
}

function diffCmds(base: string): string[][] {
  return base === 'none'
    ? [['diff', '--binary', '--cached'], ['diff', '--binary']]
    : [['diff', '--binary', base]];
}

export async function workspacePolicy(cwd: string): Promise<{ paperCut: boolean }> {
  const fromConfig = (await soft(cwd, ['config', '--get', 'user.email'])).trim();
  const fromVar = ((await soft(cwd, ['var', 'GIT_COMMITTER_IDENT'])).match(/<([^<>]*)>/)?.[1] ?? '').trim();
  const email = (fromConfig || fromVar).toLowerCase();
  const remotes = await soft(cwd, ['config', '--get-regexp', 'remote\\..*\\.url']);
  const paperCut = email.endsWith('@papercut.com') || /github\.com[:/]PaperCutSoftware\//i.test(remotes) || /gitlab[^\s]*papercut/i.test(remotes);
  return { paperCut };
}

export async function head(cwd: string): Promise<string> {
  try {
    return (await git(cwd, ['rev-parse', '--verify', 'HEAD'])).trim() || 'none';
  } catch {
    return 'none';
  }
}

export async function snapshot(cwd: string, base: string): Promise<string> {
  const status = await git(cwd, ['status', '--porcelain', '--untracked-files=all']);
  const diffs = [];
  for (const args of diffCmds(base)) diffs.push([args.join(' '), await git(cwd, args)] as const);

  const hash = createHash('sha256');
  let bytes = Buffer.byteLength(status);
  hash.update('base\0');
  hash.update(base);
  hash.update('\0status\0');
  hash.update(status);
  hash.update('\0');
  for (const [cmd, out] of diffs) {
    bytes += Buffer.byteLength(out);
    if (bytes > MAX_BYTES) throw new Error('Workspace snapshot exceeds the 50 MiB limit');
    hash.update(`diff\0${cmd}\0`);
    hash.update(out);
    hash.update('\0');
  }

  for (const rel of await untracked(cwd)) {
    const e = await inspect(cwd, rel);
    hash.update(`untracked\0${rel}\0${e.kind}\0`);
    if (e.kind === 'link') { hash.update(await readlink(e.abs)); continue; }
    if (e.kind !== 'file') throw new Error(`Cannot snapshot irregular entry '${rel}'`);
    if (e.size > MAX_BYTES) throw new Error(`'${rel}' exceeds the 50MB limit`);
    bytes += e.size;
    if (bytes > MAX_BYTES) throw new Error('Workspace snapshot exceeds the 50 MiB limit');
    hash.update(await readNoFollow(e.abs));
    hash.update('\0');
  }
  return hash.digest('hex');
}

export async function changes(cwd: string, base: string, limit = 60_000): Promise<string> {
  if (!Number.isFinite(limit) || limit <= 0) throw new Error(`limit must be a positive integer, got ${limit}`);
  const out: string[] = [];
  let used = 0;
  let omitted = 0;

  const emit = (label: string, body: string): void => {
    const chunk = `### ${label}\n${body}\n`;
    const room = limit - used;
    if (room <= 0) {
      omitted += chunk.length;
      return;
    }
    out.push(room >= chunk.length ? chunk : `${chunk.slice(0, room)}\n[truncated]\n`);
    used = Math.min(limit, used + chunk.length);
  };

  emit('git status --porcelain --untracked-files=all', await git(cwd, ['status', '--porcelain', '--untracked-files=all']));
  for (const args of diffCmds(base).map((a) => a.filter((x) => x !== '--binary'))) {
    emit(`git ${args.join(' ')}`, await git(cwd, args));
  }

  for (const rel of await untracked(cwd)) {
    if (used >= limit) { out.push('\n[remaining new-file content omitted]\n'); break; }
    const e = await inspect(cwd, rel);
    if (e.kind === 'link') {
      emit(`untracked ${rel}`, '<symlink: contents skipped>');
      continue;
    }
    if (e.kind === 'irregular') {
      emit(`untracked ${rel}`, '<not a regular file: contents skipped>');
      continue;
    }
    if (e.size > MAX_BYTES) throw new Error(`'${rel}' exceeds the 50MB limit`);
    const buf = await readNoFollow(e.abs);
    if (isBinary(buf)) {
      emit(`untracked ${rel}`, `<binary: ${buf.length} bytes skipped>`);
      continue;
    }
    emit(`untracked ${rel}`, buf.toString('utf8'));
  }

  if (omitted > 0) out.push(`\n[truncated: ${omitted} more characters omitted]\n`);
  return out.join('');
}
