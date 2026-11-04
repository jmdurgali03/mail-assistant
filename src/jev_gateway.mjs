// Fixed-purpose bridge to the installed official decision provider. No API key,
// model-generated commands, shell, chat turn, or provider HTTP implementation.
import { homedir } from 'node:os';
import { pathToFileURL } from 'node:url';
import { readFile } from 'node:fs/promises';

try {
  let input = '';
  process.stdin.setEncoding('utf8');
  for await (const chunk of process.stdin) {
    input += chunk;
    if (Buffer.byteLength(input) > 24000) throw new Error('input too large');
  }
  const batch = JSON.parse(input);
  if (!batch || Object.keys(batch).sort().join(',') !== 'questions,state') {
    throw new Error('invalid batch');
  }
  // Refuse configured model/endpoint drift before sending evidence. The Gateway
  // remains the credential owner; this reads only config, never the Secret Store.
  const config = JSON.parse(await readFile(homedir() + '/.openclaw/openclaw.json', 'utf8'));
  const selection = config.agents?.entries?.['main']?.decisionModel ??
    config.agents?.defaults?.decisionModel;
  const plugin = config.plugins?.entries?.typesafe;
  if (selection !== 'typesafe/jev-1.13.0' || plugin?.enabled !== true ||
      plugin.config?.baseUrl || plugin.config?.apiKey?.source !== 'store' ||
      plugin.config.apiKey.id !== 'TYPESAFE_API_KEY') throw new Error('configuration drift');
  const sdk = pathToFileURL(homedir() +
    '/.npm-global/lib/node_modules/openclaw/dist/plugin-sdk/gateway-runtime.js');
  const { callGatewayFromCli } = await import(sdk.href);
  const response = await callGatewayFromCli('tools.invoke', { timeout: '35000', json: true }, {
    name: 'decision_evaluate',
    sessionKey: 'agent:main:mail-assistant-decisions',
    args: batch,
  }, { signal: AbortSignal.timeout(35000) });
  if (response.ok !== true) throw new Error('gateway rejected');
  // Return only the typed outcome, never tool wrappers or raw gateway errors.
  const output = response.output;
  const outcome = output.details ?? output.structuredContent ??
    JSON.parse(output.content.find(part => part.type === 'text').text);
  process.stdout.write(JSON.stringify(outcome));
} catch {
  process.stdout.write(JSON.stringify({ status: 'unavailable', reason: 'gateway-unavailable' }));
  process.exitCode = 1;
}
