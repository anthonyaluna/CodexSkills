import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import test from 'node:test';

const patternsPath = new URL(
  '../skills/.curated/cloudflare-deploy/references/pages/patterns.md',
  import.meta.url,
);

async function loadDocumentedAuth() {
  const markdown = await readFile(patternsPath, 'utf8');
  const match = markdown.match(/## Auth Middleware\s+```typescript\s*([\s\S]*?)```/);
  assert.ok(match, 'Auth Middleware TypeScript example exists');

  const source = match[1]
    .replace(
      'const auth: PagesFunction<Env> = async (context) => {',
      'const auth = async (context) => {',
    )
    .replace('export const onRequest = [auth];', 'auth;');

  const verifyCalls = [];
  const auth = runInNewContext(source, {
    URL,
    Response,
    verifyJWT: async (token, secret) => {
      verifyCalls.push({ token, secret });
      return { sub: 'user-1' };
    },
  });

  return { auth, verifyCalls };
}

function makeContext(url, authorization) {
  let nextCalls = 0;
  const headers = new Map(authorization ? [['Authorization', authorization]] : []);

  return {
    context: {
      request: { url, headers: { get: (name) => headers.get(name) ?? null } },
      data: {},
      env: { JWT_SECRET: 'test-secret' },
      next: async () => {
        nextCalls += 1;
        return new Response('next');
      },
    },
    nextCalls: () => nextCalls,
  };
}

test('query path markers do not bypass JWT verification', async () => {
  for (const url of [
    'https://example.test/admin?next=/public/health',
    'https://example.test/admin?next=%2Fpublic%2Fhealth',
  ]) {
    const { auth, verifyCalls } = await loadDocumentedAuth();
    const request = makeContext(url);

    const response = await auth(request.context);

    assert.equal(response.status, 401, url);
    assert.equal(request.nextCalls(), 0, url);
    assert.equal(verifyCalls.length, 0, url);
  }
});

test('public path exemptions remain public', async () => {
  const { auth, verifyCalls } = await loadDocumentedAuth();
  const request = makeContext('https://example.test/public/health');

  const response = await auth(request.context);

  assert.equal(response.status, 200);
  assert.equal(await response.text(), 'next');
  assert.equal(request.nextCalls(), 1);
  assert.equal(verifyCalls.length, 0);
});

test('protected routes still verify a valid bearer token', async () => {
  const { auth, verifyCalls } = await loadDocumentedAuth();
  const request = makeContext('https://example.test/admin', 'Bearer token-1');

  const response = await auth(request.context);

  assert.equal(response.status, 200);
  assert.deepEqual(verifyCalls, [{ token: 'token-1', secret: 'test-secret' }]);
  assert.deepEqual(request.context.data.user, { sub: 'user-1' });
  assert.equal(request.nextCalls(), 1);
});
