/**
 * 使用 Playwright 连接 Browserbase 云浏览器
 * 需设置环境变量 BROWSERBASE_API_KEY、BROWSERBASE_PROJECT_ID
 * 支持代理：options.proxy 为 true 时使用内置代理（默认 US），
 * 或 options.proxy 为 { country, city?, state? } 时按地理定位使用 Browserbase 代理。
 * @see https://docs.browserbase.com/features/proxies
 */
if (typeof globalThis.fetch === 'undefined') {
  try {
    globalThis.fetch = require('node-fetch');
  } catch (e) {
    // Node 18+ 自带 fetch，无需 polyfill
  }
}
const { chromium } = require('playwright-core');

function buildProxiesOption(proxy) {
  if (!proxy) return {};
  if (proxy === true) return { proxies: true };
  if (typeof proxy === 'object' && proxy.country) {
    const geolocation = { country: String(proxy.country).toUpperCase().replace(/-/g, '_') };
    if (proxy.city) geolocation.city = String(proxy.city).toUpperCase().replace(/\s+/g, '_');
    if (proxy.state) geolocation.state = String(proxy.state).toUpperCase().replace(/\s+/g, '_');
    return { proxies: [{ type: 'browserbase', geolocation }] };
  }
  return {};
}

async function createBrowser(options = {}) {
  const apiKey = process.env.BROWSERBASE_API_KEY;
  const projectId = process.env.BROWSERBASE_PROJECT_ID;
  if (!apiKey || !projectId) {
    throw new Error('Missing BROWSERBASE_API_KEY or BROWSERBASE_PROJECT_ID');
  }

  const sessionOptions = { projectId, ...buildProxiesOption(options.proxy) };
  let session;
  try {
    const SDK = require('@browserbasehq/sdk');
    if (typeof SDK.createSession === 'function') {
      session = await SDK.createSession({ ...sessionOptions, apiKey });
    } else {
      const Browserbase = SDK.default || SDK.Browserbase || SDK;
      const bb = typeof Browserbase === 'function' ? new Browserbase({ apiKey }) : Browserbase;
      if (bb.sessions && typeof bb.sessions.create === 'function') {
        session = await bb.sessions.create(sessionOptions);
      } else if (typeof bb.createSession === 'function') {
        session = await bb.createSession(sessionOptions);
      } else if (typeof bb.sessionCreate === 'function') {
        session = await bb.sessionCreate(sessionOptions);
      } else {
        throw new Error('Browserbase SDK 未提供 sessions.create / createSession，请检查 @browserbasehq/sdk 版本');
      }
    }
  } catch (e) {
    if (e.message && e.message.indexOf('Browserbase') !== -1) throw e;
    throw new Error('Please install @browserbasehq/sdk: npm install @browserbasehq/sdk. ' + e.message);
  }
  const connectUrl = session && (
    session.connectUrl ||
    session.connect_url ||
    session.connectionUrl ||
    session.connection_url ||
    (session.data && (session.data.connectUrl || session.data.connect_url)) ||
    (session.result && (session.result.connectUrl || session.result.connect_url))
  );
  if (!connectUrl) {
    if (session && (session.statusCode || session.error || session.message)) {
      const msg = [session.message, session.error].filter(Boolean).join(' ') || ('statusCode: ' + session.statusCode);
      throw new Error('Browserbase API 报错: ' + msg);
    }
    const keys = session ? Object.keys(session) : [];
    throw new Error('Browserbase session 未返回 connectUrl（返回字段: ' + keys.join(', ') + '）。请检查 BROWSERBASE_PROJECT_ID 是否正确、项目是否已启用。');
  }
  const browser = await chromium.connectOverCDP(connectUrl);
  return browser;
}

module.exports = { createBrowser, buildProxiesOption };
