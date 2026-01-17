#!/usr/bin/env node

/**
 * DrSpec postinstall script
 * Downloads the correct platform-specific binary from GitHub releases
 */

const https = require('https');
const fs = require('fs');
const path = require('path');

const VERSION = require('./package.json').version;
const BINARY_HOST = 'https://github.com/drspec-ai/drspec/releases/download';

// Platform detection mapping
const PLATFORMS = {
  'darwin-x64': 'drspec-macos-x64',
  'darwin-arm64': 'drspec-macos-arm64',
  'linux-x64': 'drspec-linux-x64',
  'linux-arm64': 'drspec-linux-arm64',
  'win32-x64': 'drspec-windows-x64.exe'
};

/**
 * Get the current platform key (e.g., 'darwin-arm64')
 */
function getPlatformKey() {
  return `${process.platform}-${process.arch}`;
}

/**
 * Download a file from a URL, following redirects
 * @param {string} url - The URL to download from
 * @param {string} dest - The destination file path
 * @returns {Promise<void>}
 */
function download(url, dest) {
  return new Promise((resolve, reject) => {
    const file = fs.createWriteStream(dest);

    const request = https.get(url, (response) => {
      // Handle redirects (GitHub releases use 302 redirects to CDN)
      if (response.statusCode === 301 || response.statusCode === 302) {
        file.close();
        fs.unlinkSync(dest);
        return download(response.headers.location, dest)
          .then(resolve)
          .catch(reject);
      }

      if (response.statusCode !== 200) {
        file.close();
        fs.unlinkSync(dest);
        reject(new Error(`HTTP ${response.statusCode}: ${response.statusMessage}`));
        return;
      }

      response.pipe(file);

      file.on('finish', () => {
        file.close();
        resolve();
      });

      file.on('error', (err) => {
        fs.unlinkSync(dest);
        reject(err);
      });
    });

    request.on('error', (err) => {
      file.close();
      fs.unlinkSync(dest);
      reject(err);
    });

    request.setTimeout(60000, () => {
      request.destroy();
      file.close();
      fs.unlinkSync(dest);
      reject(new Error('Download timed out'));
    });
  });
}

/**
 * Main installation function
 */
async function install() {
  const platformKey = getPlatformKey();
  const binaryName = PLATFORMS[platformKey];

  if (!binaryName) {
    console.error(`\nError: Unsupported platform: ${platformKey}`);
    console.error('\nSupported platforms:');
    console.error('  - darwin-x64    (macOS Intel)');
    console.error('  - darwin-arm64  (macOS Apple Silicon)');
    console.error('  - linux-x64     (Linux x86_64)');
    console.error('  - linux-arm64   (Linux ARM64)');
    console.error('  - win32-x64     (Windows x64)');
    console.error('\nAlternatively, install via pip: pip install drspec');
    process.exit(1);
  }

  const url = `${BINARY_HOST}/v${VERSION}/${binaryName}`;
  const binDir = path.join(__dirname, 'bin');
  const destName = process.platform === 'win32' ? 'drspec.exe' : 'drspec-bin';
  const dest = path.join(binDir, destName);

  // Ensure bin directory exists
  if (!fs.existsSync(binDir)) {
    fs.mkdirSync(binDir, { recursive: true });
  }

  console.log(`\nDrSpec v${VERSION}`);
  console.log(`Platform: ${platformKey}`);
  console.log(`Downloading: ${binaryName}...`);

  try {
    await download(url, dest);

    // Make executable on Unix systems
    if (process.platform !== 'win32') {
      fs.chmodSync(dest, 0o755);
    }

    console.log('\nDrSpec installed successfully!');
    console.log('\nGet started:');
    console.log('  drspec init       # Initialize in current directory');
    console.log('  drspec scan ./src # Scan source files');
    console.log('  drspec status     # Check project status\n');
  } catch (err) {
    console.error(`\nFailed to download binary: ${err.message}`);
    console.error('\nTroubleshooting:');
    console.error('  1. Check your internet connection');
    console.error('  2. Verify the release exists: ' + url);
    console.error('  3. Install via pip instead: pip install drspec');
    process.exit(1);
  }
}

// Run installation
install();
