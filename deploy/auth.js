// One shared password for the whole site, checked by nginx (njs) on every request.
// HTTP Basic auth: the browser asks once and remembers it. Any username works.
// SITE_PASSWORD comes from the environment (`env SITE_PASSWORD;` in nginx.conf).

function same(a, b) {
  // constant time for equal lengths
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// "1" when the request carries the right password, "" otherwise (used by js_set).
function authorised(r) {
  const want = process.env.SITE_PASSWORD || '';
  const header = r.headersIn.Authorization || '';
  if (!want || !header.startsWith('Basic ')) return '';
  let creds;
  try {
    creds = Buffer.from(header.slice(6).trim(), 'base64').toString();
  } catch (e) {
    return '';
  }
  const colon = creds.indexOf(':');
  return colon >= 0 && same(creds.slice(colon + 1), want) ? '1' : '';
}

export default { authorised };
