import globals from 'globals';

export default [{
  files: ['web/js/**/*.js', 'tests/frontend/**/*.js', 'playwright.config.js'],
  languageOptions: { globals: { ...globals.browser, ...globals.node } },
  rules: {
    'no-undef': 'error',
    'no-unused-vars': ['error', { args: 'none', caughtErrors: 'none' }],
    'no-duplicate-imports': 'error',
  },
}];
