const nextJest = require('next/jest.js').default ?? require('next/jest.js');

// Ensure NODE_ENV=test so react-dom/test-utils loads the development build
// (the production build removed React.act in React 19, causing flaky failures).
process.env.NODE_ENV = 'test';

const createJestConfig = nextJest({
  dir: './',
});

const config = {
  displayName: '@danieljoffe.com/wyrdfold',
  preset: '../../jest.preset.js',
  transform: {
    '^(?!.*\\.(js|jsx|ts|tsx|css|json)$)': '@nx/react/plugins/jest',
  },
  moduleFileExtensions: ['ts', 'tsx', 'js', 'jsx'],
  coverageDirectory: '../../coverage/apps/wyrdfold',
  testEnvironment: 'jsdom',
  forceExit: true,
  moduleNameMapper: {
    '^@/(.*)$': '<rootDir>/src/$1',
  },
};

module.exports = createJestConfig(config);
