from pathlib import Path
import importlib.util
import json
import tempfile
import unittest
import zipfile

MODULE = Path(__file__).with_name('build_review_package.py')
spec = importlib.util.spec_from_file_location('review_package', MODULE)
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class ReviewPackageTests(unittest.TestCase):
    def test_selection_excludes_private_weights_and_scratch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ['README.md', 'SenseHome-6G/net_offload/main.c',
                     'SenseHome-6G/net_offload/.env', 'SenseHome-6G/net_offload/a.private.json',
                     'SenseHome-6G/net_offload/key.PEM', 'SenseHome-6G/net_offload/model.gguf',
                     'SenseHome-6G/compliance/LICENSE',
                     'SenseHome-6G/net_offload/_work/log.json',
                     'docs/deployment/fused_eval.py', 'docs/deployment/__pycache__/cache.pyc',
                     'dist/final/20261111/final.pdf', 'dist/final/20261111/old.zip']
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'data')
            selected = [p.relative_to(root).as_posix() for p in package.select_files(root)]
            self.assertEqual(set(selected), {'README.md', 'SenseHome-6G/net_offload/main.c',
                                             'SenseHome-6G/compliance/LICENSE',
                                             'docs/deployment/fused_eval.py', 'dist/final/20261111/final.pdf'})

    def test_archive_hashes_match_content_and_overwrite_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'README.md').write_text('example', encoding='utf-8')
            out = root / 'review.zip'
            report = package.build(root, out)
            self.assertTrue(report['zip_crc_and_hashes_passed'])
            with zipfile.ZipFile(out) as archive:
                self.assertEqual(archive.read('SenseHome-review/README.md'), b'example')
                self.assertIn('README.md', json.loads(archive.read('SenseHome-review/SHA256SUMS.json')))
            with self.assertRaises(FileExistsError):
                package.build(root, out)


if __name__ == '__main__':
    unittest.main()
