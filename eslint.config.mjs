import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import { FlatCompat } from '@eslint/eslintrc';

const compat = new FlatCompat({ baseDirectory: import.meta.dirname });

export default tseslint.config(
  {
    ignores: [
      '**/node_modules/**',
      '**/.next/**',
      '**/.next-qa/**',
      '**/dist/**',
      '**/.turbo/**',
      // Generated: Next.js rewrites next-env.d.ts, openapi-typescript writes the contract types.
      '**/next-env.d.ts',
      'packages/contracts/src/schema.d.ts',
      'apps/api/**',
      'apps/parser/**',
      'apps/render/**',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...compat.extends('next/core-web-vitals'),
);
