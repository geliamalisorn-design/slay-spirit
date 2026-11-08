import { copyFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = path.join(root, 'web');
const output = path.join(root, 'dist');
const files = ['index.html', 'app.js', 'styles.css', 'tutorial-practice.js'];

await mkdir(output, { recursive: true });
for (const file of files) {
  await copyFile(path.join(source, file), path.join(output, file));
}
console.log(`Built static frontend in ${path.relative(root, output)} (${files.length} files).`);
