import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import plugin, {createHandler} from './index.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
function fixture(t) {
  const dir = mkdtempSync(join(tmpdir(), 'mail-actions-test-'));
  t.after(() => rmSync(dir, {recursive: true, force: true}));
  const config = {notification_actions_enabled: true, telegram_account_id: 'default',
    telegram_chat_id: '123', telegram_user_id: '123'};
  const options = {assistantRoot: root, assistantConfig: join(dir, 'watch.json'), database: join(dir, 'watch.sqlite3')};
  writeFileSync(options.assistantConfig, JSON.stringify(config), {mode: 0o600});
  const calls = [];
  const ctx = {channel: 'telegram', accountId: 'default', callbackId: 'click-1',
    conversationId: '123', senderId: '123', isGroup: false, auth: {isAuthorizedSender: true},
    callback: {chatId: '123', messageId: 501, data: 'bm2:later:' + 'a'.repeat(24)},
    respond: {editButtons: async value => calls.push(['buttons', value]), reply: async value => calls.push(['reply', value])}};
  return {dir, config, options, ctx, calls};
}

test('registers only native Telegram namespace, without polling or model hooks', () => {
  const registered = [];
  plugin.register({pluginConfig: {assistantRoot: root, assistantConfig: '/tmp/watch.json', database: '/tmp/watch.sqlite3'},
    registerInteractiveHandler: x => registered.push(x)});
  assert.equal(registered.length, 1);
  assert.equal(registered[0].namespace, 'bm2');
  assert.equal(registered[0].channel, 'telegram');
});

test('wrong identities and revoked config never reach subprocess', async t => {
  const f = fixture(t);
  const handler = createHandler(f.options, () => { throw new Error('must not execute'); });
  for (const change of [{accountId: 'albert'}, {senderId: '999'}, {isGroup: true},
    {auth: {isAuthorizedSender: false}}, {conversationId: '999'}, {channel: 'webchat'},
    {callback: {...f.ctx.callback, chatId: '999'}}]) {
    assert.deepEqual(await handler({...f.ctx, ...change}), {handled: true});
  }
  writeFileSync(f.options.assistantConfig, JSON.stringify({...f.config, notification_actions_enabled: false}));
  await handler(f.ctx);
  assert.equal(f.calls.length, 0);
});

test('later opens duration menu, done removes state-changing buttons', async t => {
  const f = fixture(t);
  let status = 'later';
  const handler = createHandler(f.options, async (file, args, input) => {
    assert.equal(file, '/usr/bin/python3');
    assert.equal(input.user, f.ctx.senderId);
    return {external_writes: 0, status, token: 'a'.repeat(24), url: 'https://mail.google.com/mail/?authuser=x'};
  });
  await handler(f.ctx);
  assert.equal(f.calls[0][1].buttons[0][0].text, 'En 1 hora');
  assert.equal(f.calls[0][1].buttons[0][1].text, 'Mañana 08:30');
  status = 'cancel';
  await handler(f.ctx);
  assert.deepEqual(f.calls[1][1].buttons[0].map(b => b.text), ['Abrir', 'Revisado', 'Después']);
  status = 'reviewed';
  await handler(f.ctx);
  assert.deepEqual(f.calls[2][1].buttons, [[{text: 'Abrir', url: 'https://mail.google.com/mail/?authuser=x'}]]);
  assert.match(f.calls[3][1].text, /Gmail no se modificó/);
});

test('real Node handler -> Python CLI -> temporary SQLite review and replay', async t => {
  const f = fixture(t);
  const output = execFileSync('/usr/bin/python3', ['-c', `
import json,sys
from pathlib import Path
from assistant import Assistant
from mail_watch import Watch,normalize
from test_mail_watch import CFG,NOW,metadata
from pipeline import deliver
cfg={**CFG,'notification_actions_enabled':True}
Path(sys.argv[1]).write_text(json.dumps(cfg))
app=Assistant(sys.argv[2],cfg)
watch=Watch(app,clock=lambda:NOW)
watch.arm(NOW)
class Reader:
 def capture(self,*args): return [normalize(metadata())]
watch.capture(Reader(),NOW)
with app.db: watch._notice('abc1','Correo sintético.')
class Sender:
 def send(self,row): return '501'
deliver(watch.box,Sender(),NOW)
print(app.db.execute('select token from notification_tokens').fetchone()[0])
app.close()
`, f.options.assistantConfig, f.options.database], {cwd: root, encoding: 'utf8'}).trim();
  f.ctx.callback.data = `bm2:done:${output}`;
  const handler = createHandler(f.options);
  await handler(f.ctx);
  await handler(f.ctx);
  assert.equal(f.calls.filter(x => x[0] === 'reply').length, 2);
  assert.ok(f.calls.filter(x => x[0] === 'reply').every(x => /Marcado como revisado/.test(x[1].text)));
  const state = execFileSync('/usr/bin/python3', ['-c', `
import sqlite3,sys,json
c=sqlite3.connect(sys.argv[1])
print(json.dumps([c.execute('select open from mail').fetchone()[0],c.execute('select count(*) from notification_callbacks').fetchone()[0]]))
`, f.options.database], {encoding: 'utf8'});
  assert.deepEqual(JSON.parse(state), [0, 1]);
});
