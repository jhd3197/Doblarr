import assert from 'node:assert/strict';
import { test } from 'node:test';
import { baseLanguage, languageMatches } from '../../web/js/voice-picker.js';

test('regional locales match voices tagged with the base language', () => {
  assert.equal(baseLanguage('es-MX'), 'es');
  assert.equal(baseLanguage('es-419'), 'es');
  assert.equal(baseLanguage('EN'), 'en');
  assert.equal(baseLanguage('es'), 'es');
  assert.ok(languageMatches('es', 'es-MX'));
  assert.ok(languageMatches('es', 'es-VE'));
  assert.ok(languageMatches('es', 'es-419'));
  assert.ok(languageMatches('es', 'es'));
  assert.ok(!languageMatches('en', 'es-MX'));
  assert.ok(!languageMatches('es-MX', 'en'));
});
