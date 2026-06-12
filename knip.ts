import type { KnipConfig } from 'knip';

const config: KnipConfig = {
  ignoreBinaries: [
    // Python package manager used in apps/wyrdfold-api project.json targets
    'uv',
  ],
  workspaces: {
    // -----------------------------------------------------------------
    // Root workspace (configs only — no scripts/ dir in this repo)
    // -----------------------------------------------------------------
    '.': {
      entry: [],
      project: [],
      // Root jest.config delegates to Nx projects; disable to avoid
      // Next.js pages-dir lookup error at workspace root
      jest: false,
      ignoreDependencies: [
        // Tailwind v4 uses @import in CSS, not JS imports
        'tailwindcss',
        '@tailwindcss/typography',
        // caniuse-lite is required by browserslist (package.json config)
        'caniuse-lite',
        // ts-node is referenced in tsconfig.json "ts-node" block
        'ts-node',
        // @swc/cli is the SWC compiler driver referenced via apps/*/.swcrc
        '@swc/cli',
        // @eslint/js is required by @nx/eslint-plugin flat config presets
        '@eslint/js',
        // eslint-plugin-react-hooks is loaded transitively by @nx/eslint-plugin
        // flat config presets — not imported directly anywhere in our configs
        'eslint-plugin-react-hooks',
        // ts-jest is required by @nx/jest/preset transform config
        'ts-jest',
        // babel-jest is the Jest transformer fallback used by next/jest
        'babel-jest',
        // jest-util is a transitive Jest internal pulled in by next/jest
        'jest-util',
        // @nx/s3-cache is loaded at runtime via the `s3` block in nx.json
        // (Cloudflare R2 remote cache), not via a static import.
        '@nx/s3-cache',
        // eslint-config-next + these plugins are required by name by the
        // @nx/eslint-plugin flat/react-typescript preset, not imported directly.
        'eslint-config-next',
        'eslint-plugin-import',
        'eslint-plugin-jsx-a11y',
        'eslint-plugin-react',
        // @swc/jest is the Jest transform wired via the @nx/jest preset
        '@swc/jest',
        // jsdom is the engine behind jest-environment-jsdom (testEnvironment)
        'jsdom',
        // playwright provides the browser binaries CLI alongside @playwright/test
        'playwright',
        // lint-staged is invoked by the husky pre-commit hook + its own config block
        'lint-staged',
        // sharp is used by next/image at runtime (root devDep mirrors apps/wyrdfold)
        'sharp',
      ],
    },

    // -----------------------------------------------------------------
    // apps/wyrdfold — Next.js 16 application (WyrdFold product)
    // -----------------------------------------------------------------
    'apps/wyrdfold': {
      entry: [
        'src/app/**/page.tsx',
        'src/app/**/layout.tsx',
        'src/app/**/loading.tsx',
        'src/app/**/error.tsx',
        'src/app/**/not-found.tsx',
        'src/app/**/route.ts',
        'src/app/api/**/route.ts',
        // Module declaration for *.svg imports — referenced via tsconfig include
        'index.d.ts',
        // Placeholder kept for future regen via `supabase gen types`
        'src/lib/supabase/types.ts',
      ],
      project: ['src/**/*.{ts,tsx}'],
      ignoreDependencies: [
        // sharp is used internally by next/image at runtime
        'sharp',
      ],
    },

    // -----------------------------------------------------------------
    // apps/wyrdfold-e2e — Playwright E2E tests for wyrdfold
    // -----------------------------------------------------------------
    'apps/wyrdfold-e2e': {
      // Playwright spec entry points: ``.spec.ts`` files and
      // ``auth.setup.ts`` (referenced by the ``setup`` project's
      // ``testMatch`` regex in playwright.config.ts).
      entry: ['src/**/*.spec.ts', 'src/**/*.setup.ts'],
      project: ['src/**/*.ts'],
    },
  },
};

export default config;
