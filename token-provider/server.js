import http from 'node:http';
import { JSDOM } from 'jsdom';
import { BotGuardClient, getChallenge } from 'bgutils-js/botguard';
import { WebPoMinter } from 'bgutils-js/webpo';
import { buildURL, getHeaders, USER_AGENT } from 'bgutils-js/utils';

const HOST = process.env.POT_HOST || '127.0.0.1';
const PORT = Number(process.env.POT_PORT || 8878);
const SECRET = process.env.KANGODL_POT_SECRET || '';
const REQUEST_KEY = 'O43z0dpjhgX20SCx4KAo';

if (!SECRET) {
  console.error('KANGODL_POT_SECRET is required; refusing to start without internal authentication.');
  process.exit(1);
}

let minterState = null;
let initPromise = null;

async function createMinter() {
  const dom = new JSDOM('<!DOCTYPE html><html lang="en"><head></head><body></body></html>', {
    url: 'https://www.youtube.com/',
    referrer: 'https://www.youtube.com/',
    userAgent: USER_AGENT
  });

  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    location: dom.window.location,
    origin: dom.window.origin
  });
  if (!Reflect.has(globalThis, 'navigator')) {
    Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true });
  }

  const challenge = await getChallenge({ fetchFunction: fetch, requestKey: REQUEST_KEY });
  const interpreter = challenge.interpreterJavascript?.privateDoNotAccessOrElseSafeScriptWrappedValue;
  if (!interpreter) throw new Error('BotGuard interpreter script was not returned');

  new Function(interpreter)();
  const client = await BotGuardClient.create({
    program: challenge.program,
    globalName: challenge.globalName,
    globalObject: globalThis
  });

  const webPoSignalOutput = [];
  const botguardResponse = await client.snapshot({ webPoSignalOutput });
  const response = await fetch(buildURL('GenerateIT', true), {
    method: 'POST',
    headers: getHeaders(),
    body: JSON.stringify([REQUEST_KEY, botguardResponse])
  });
  if (!response.ok) throw new Error(`GenerateIT failed with HTTP ${response.status}`);

  const data = await response.json();
  if (!Array.isArray(data) || !data[0]) throw new Error('GenerateIT returned no integrity token');
  const [integrityToken, estimatedTtlSecs] = data;
  const minter = await WebPoMinter.create({
    integrityToken,
    estimatedTtlSecs,
    mintRefreshThreshold: data[2],
    websafeFallbackToken: data[3]
  }, webPoSignalOutput);

  const ttl = Math.max(60, Number(estimatedTtlSecs) || 3600);
  minterState = { minter, expiresAt: Date.now() + Math.floor(ttl * 0.75) * 1000, ttl };
  dom.window.close();
  return minterState;
}

async function getMinter() {
  if (minterState && Date.now() < minterState.expiresAt) return minterState;
  if (!initPromise) {
    initPromise = createMinter().finally(() => { initPromise = null; });
  }
  return initPromise;
}

async function readJson(req) {
  let raw = '';
  for await (const chunk of req) {
    raw += chunk;
    if (raw.length > 8192) throw new Error('Request body too large');
  }
  return JSON.parse(raw || '{}');
}

const server = http.createServer(async (req, res) => {
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  if (req.method === 'GET' && req.url === '/health') {
    res.writeHead(200).end(JSON.stringify({ ok: true, service: 'KangoDL PO Token Provider' }));
    return;
  }
  if (req.method !== 'POST' || req.url !== '/token') {
    res.writeHead(404).end(JSON.stringify({ ok: false, error: 'Not found' }));
    return;
  }
  if (req.headers.authorization !== `Bearer ${SECRET}`) {
    res.writeHead(401).end(JSON.stringify({ ok: false, error: 'Unauthorized' }));
    return;
  }
  try {
    const body = await readJson(req);
    const videoId = String(body.videoId || '');
    if (!/^[A-Za-z0-9_-]{11}$/.test(videoId)) {
      res.writeHead(400).end(JSON.stringify({ ok: false, error: 'A valid 11-character videoId is required' }));
      return;
    }
    const state = await getMinter();
    const poToken = await state.minter.mintAsWebsafeString(videoId);
    if (!poToken) throw new Error('PO token minting returned an empty value');
    res.writeHead(200).end(JSON.stringify({
      ok: true, videoId, client: 'WEB', tokenType: 'GVS content-bound',
      poToken, provider: 'bgutils-js', ttlSeconds: Math.max(0, Math.floor((state.expiresAt - Date.now()) / 1000))
    }));
  } catch (error) {
    console.error('Token request failed:', error?.stack || error);
    res.writeHead(502).end(JSON.stringify({ ok: false, error: String(error?.message || error) }));
  }
});

server.listen(PORT, HOST, () => console.log(`KangoDL PO Token Provider listening on http://${HOST}:${PORT}`));
