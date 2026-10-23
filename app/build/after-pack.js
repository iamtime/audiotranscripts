// Without a Developer ID (VOICE_REC_ADHOC=1, see packaging/build_app.sh) sign the
// app ad hoc: Apple Silicon refuses unsigned code, and a bundle whose seal was
// broken by packaging shows as "damaged" instead of "unidentified developer".
const { execFileSync } = require('child_process');
const path = require('path');

exports.default = async function afterPack(context) {
  if (process.env.VOICE_REC_ADHOC !== '1' || context.electronPlatformName !== 'darwin') return;
  const appPath = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  console.log(`  • ad-hoc signing ${appPath}`);
  execFileSync('codesign', ['--force', '--deep', '--sign', '-', '--timestamp=none',
    '--entitlements', path.join(__dirname, 'entitlements.mac.plist'), appPath], { stdio: 'inherit' });
};
