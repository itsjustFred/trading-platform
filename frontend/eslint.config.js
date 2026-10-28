import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
  },
  {
    // `react-hooks/incompatible-library` only reports APIs that the React
    // Compiler's inferred memoization must not wrap; the compiler bails out of
    // such a component instead of miscompiling it. This project does not run the
    // compiler (no babel-plugin-react-compiler, plain @vitejs/plugin-react in
    // vite.config.ts), and the log table has to stay virtualized: it renders
    // every line of a log file, so @tanstack/react-virtual is not optional.
    // Scoped to the one call site so the rule still guards every other usage.
    files: ['src/pages/LoggingPage.tsx'],
    rules: {
      'react-hooks/incompatible-library': 'off',
    },
  },
])
