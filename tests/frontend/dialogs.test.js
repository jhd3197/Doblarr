import assert from 'node:assert/strict';
import { test } from 'node:test';
import { confirmDialog, currentDialog, promptDialog, settleDialog } from '../../ui/src/lib/dialogs.js';

test('dialogs queue and settle one at a time', async () => {
  const first = confirmDialog({ title: 'Reveal?' });
  const second = promptDialog({ title: 'Copy id', defaultValue: 'harbor-copy' });
  assert.equal(currentDialog().title, 'Reveal?');
  settleDialog(true);
  assert.equal(await first, true);
  assert.equal(currentDialog().defaultValue, 'harbor-copy');
  settleDialog('mina-copy');
  assert.equal(await second, 'mina-copy');
  assert.equal(currentDialog(), null);
});

test('cancelling resolves false for a confirm and null for a prompt', async () => {
  const sure = confirmDialog({ title: 'Sure?' });
  settleDialog();
  assert.equal(await sure, false);
  const text = promptDialog({ title: 'Name' });
  settleDialog();
  assert.equal(await text, null);
});
