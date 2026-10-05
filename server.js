// GME tracker server: zero dependencies, needs Node 18+. Run: node server.js
const http = require('http'), fs = require('fs'), path = require('path');
const PORT = process.env.PORT || 3000, SYM = 'GME', UA = { 'User-Agent': 'Mozilla/5.0' };
const cache = new Map();

// Free, unofficial Yahoo Finance chart endpoint. No API key; may rate-limit or change.
async function yahoo(qs, ttl, key = qs) {
  const hit = cache.get(key);
  if (hit && Date.now() - hit.t < ttl) return hit.v;
  try {
    const r = await fetch(`https://query1.finance.yahoo.com/v8/finance/chart/${SYM}?${qs}`, { headers: UA });
    if (!r.ok) throw new Error('upstream ' + r.status);
    const v = (await r.json()).chart.result[0];
    cache.set(key, { t: Date.now(), v });
    return v;
  } catch (e) {
    if (hit) return hit.v; // serve stale rather than fail
    throw e;
  }
}

const routes = {
  '/api/quote': async () => {
    const m = (await yahoo('range=1d&interval=1m', 15000)).meta;
    return { price: m.regularMarketPrice, prev: m.chartPreviousClose, time: m.regularMarketTime };
  },
  '/api/history': async (q) => {
    const R = { '1D': ['1d', '5m'], '1W': ['7d', '30m'], '1M': ['30d', '1d'], '1Y': ['365d', '1d'], '3Y': ['1095d', '1d'], '5Y': ['1825d', '1d'] }[q.get('range')] || ['30d', '1d'];
    const days = parseInt(R[0]), now = Math.floor(Date.now() / 1000);
    const qs = R[0] === '1d' ? 'range=1d&interval=5m' : `period1=${now - days * 86400}&period2=${now + 86400}&interval=${R[1]}`;
    const r = await yahoo(qs, R[1] === '1d' ? 600000 : 60000, 'hist-' + R[0]);
    const c = r.indicators.quote[0].close;
    return (r.timestamp || []).map((t, i) => ({ t: t * 1000, c: c[i] })).filter(x => x.c != null);
  },
};

http.createServer(async (req, res) => {
  const u = new URL(req.url, 'http://localhost'), h = routes[u.pathname];
  if (h) {
    try {
      const body = JSON.stringify(await h(u.searchParams));
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(body);
    } catch (e) { res.writeHead(502, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: e.message })); }
  } else {
    fs.readFile(path.join(__dirname, 'public', 'index.html'), (err, d) => {
      res.writeHead(err ? 500 : 200, { 'Content-Type': 'text/html' }); res.end(err ? 'error' : d);
    });
  }
}).listen(PORT, () => console.log(`GME tracker running at http://localhost:${PORT}`));
