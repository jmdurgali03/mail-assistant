import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import plugin, {createHandler} from './index.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const config = {account: 'synthetic@example.com', incoming_notifications: 'individual_all',
  initial_sync_mode: 'baseline', telegram_account_id: 'bichi', telegram_user_id: '123',
  telegram_chat_id: '123', correction_bridge_enabled: true};
const options = {enabled: true, assistantRoot: root, assistantConfig: '/fixture/config.json', database: '/fixture/state.db'};
function pair(content = '/mail_correct ab1 {"priority":"alta"}') {
  return [{channel: 'telegram', isGroup: false, senderId: '123', messageId: '500', content},
    {channelId: 'telegram', accountId: 'bichi', conversationId: '123', senderId: '123', messageId: '500'}];
}

test('registration is inert unless explicitly enabled', () => {
  plugin.register({pluginConfig: {}, on() {assert.fail('registered while disabled');}});
  assert.equal(createHandler({enabled: false}, {}), undefined);
  for (const data of [{...config, correction_bridge_enabled: false},
    {...config, telegram_account_id: 'ALBERT-prod'}, {...config, telegram_chat_id: '-123'}]) {
    assert.throws(() => createHandler(options, data));
  }
});

test('runtime identity required; no user text identity, quoted reply or model input used', async () => {
  let count = 0;
  const hook = createHandler(options, config, async () => {count++; assert.fail('must not run');});
  for (const block of [0, 1]) {
    for (const field of Object.keys(pair()[block])) {
      if (field === 'content') continue;
      const args = pair();
      delete args[block][field];
      assert.deepEqual(await hook(...args), {handled: true});
    }
  }
  for (const patch of [{isGroup: true}, {isGroup: undefined}, {senderId: '456'}, {messageId: '501'}]) {
    const [event, ctx] = pair();
    assert.deepEqual(await hook({...event, ...patch}, ctx), {handled: true});
  }
  const [event, ctx] = pair();
  assert.equal(await hook(event, {...ctx, accountId: 'albert'}), undefined);
  assert.equal(await hook(event, {...ctx, channelId: 'webchat'}), undefined);
  assert.equal(await hook({...event, content: 'subí la prioridad de este correo'}, ctx), undefined);
  assert.equal(count, 0);
});

test('fixed IPC command and generic uncertain receipt handling', async () => {
  const hook = createHandler(options, config, async (exe, args, opts) => {
    assert.equal(exe, '/usr/bin/python3');
    assert.equal(opts.shell, false);
    assert.equal(opts.timeout, 8000);
    assert.ok(args[0].endsWith('/bridge.py'));
    assert.equal(args.includes('/mail_correct'), false);
    assert.deepEqual(JSON.parse(opts.input), {event: pair()[0], context: pair()[1]});
    return {stdout: '{"status":"recorded_locally","external_writes":0}'};
  });
  assert.equal((await hook(...pair())).handled, true);
  for (const run of [async () => {throw new Error('SYNTHETIC_PRIVATE_ERROR');},
    async () => ({stdout: '{}'}), async () => ({stdout: '{"status":"recorded_locally","external_writes":1}'})]) {
    const result = await createHandler(options, config, run)(...pair());
    assert.equal(result.handled, true);
    assert.ok(result.text.includes('No se confirmó'));
    assert.ok(!result.text.includes('SYNTHETIC'));
  }
});

test('real Node -> Python -> SQLite correction, replay, conflict and re-registration', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'bichi-bridge-'));
  try {
    const cfg = join(folder, 'config.json'), db = join(folder, 'state.db');
    writeFileSync(cfg, JSON.stringify(config), {mode: 0o600});
    execFileSync('/usr/bin/python3', ['-c', `
import json, sys
from assistant import Assistant
from test_assistant import NOW, snapshot, message
app=Assistant(sys.argv[2],json.load(open(sys.argv[1])))
app.ingest(snapshot([message('ab1')]),NOW)
app.close()
`, cfg, db], {cwd: root});
    function register() {
      let handler;
      plugin.register({pluginConfig: {...options, assistantConfig: cfg, database: db},
        on(name, fn) {assert.equal(name, 'before_dispatch'); handler = fn;}});
      return handler;
    }
    let hook = register();
    assert.ok((await hook(...pair())).text.includes('registrada localmente'));
    hook = register();
    assert.ok((await hook(...pair())).text.includes('ya estaba registrada'));
    assert.ok((await hook(...pair('/mail_correct ab1 {"priority":"media"}'))).text.includes('No se confirmó'));
    const state = JSON.parse(execFileSync('/usr/bin/python3', ['-c', `
import json,sqlite3,sys
db=sqlite3.connect(sys.argv[1])
print(json.dumps({'analysis':json.loads(db.execute('SELECT analysis FROM mail').fetchone()[0]),
 'count':db.execute('SELECT count(*) FROM corrections').fetchone()[0]}))
`, db], {encoding: 'utf8'}));
    assert.equal(state.analysis.priority, 'alta');
    assert.equal(state.count, 1);
    writeFileSync(cfg, JSON.stringify({...config, correction_bridge_enabled: false}));
    const [event, ctx] = pair();
    assert.ok((await hook({...event, messageId: '501'}, {...ctx, messageId: '501'})).text.includes('No se confirmó'));
  } finally {rmSync(folder, {recursive: true, force: true});}
});
