import json
from pathlib import Path
import tempfile
import unittest
from app_info import APP_VERSION, KUGOU_VERSION, kugou_default_mode
from tools.package_kugou import VERSION, validate_runtime, public_files


class KugouReleaseTests(unittest.TestCase):
    def test_current_kugou_guides_and_download_link_match_dedicated_version(self):
        root=Path(__file__).resolve().parents[1]
        for name in ('KUGOU.md','release/kugou-guide.txt','release/kugou-release-notes.md'):
            text=(root/name).read_text(encoding='utf-8')
            for phrase in (KUGOU_VERSION,'最小化','LOCALAPPDATA','完整解压'):
                self.assertIn(phrase,text)
        readme=(root/'README.md').read_text(encoding='utf-8')
        self.assertIn(f'releases/download/v{KUGOU_VERSION}/dancing-lyrics-{KUGOU_VERSION}-windows-x64.zip',readme)
        self.assertIn('CAR0TT0',readme)

    def test_double_click_defaults_are_scoped_to_dedicated_package(self):
        self.assertTrue(kugou_default_mode(r'D:\中文 目录\DancingLyrics-Kugou.exe', True, []))
        for exe, frozen, args in (('FloatingLyrics.exe',True,[]), ('python.exe',False,[]),
                                  ('DancingLyrics-Kugou.exe',True,['--demo']),
                                  ('DancingLyrics-Kugou.exe',True,['--netease'])):
            self.assertFalse(kugou_default_mode(exe, frozen, args))
        self.assertEqual(VERSION, KUGOU_VERSION)
        self.assertNotEqual(KUGOU_VERSION, APP_VERSION)

    def test_incomplete_qt_is_rejected_before_creating_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'DancingLyrics-Kugou.exe').write_bytes(b'exe')
            with self.assertRaisesRegex(ValueError,'QtCore.pyd'):
                validate_runtime(root)
            for module in ('Core','Gui','Widgets'):
                path=root/f'_internal/PySide6/Qt{module}.pyd'
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(b'fixture')
            validate_runtime(root)

    def test_archive_allowlist_omits_private_files_and_unrelated_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            allowed=['DancingLyrics-Kugou.exe','使用说明.txt','licenses/MIT.txt','_internal/PySide6/QtCore.pyd']
            private=['.state/settings.ini','app.log','personal.txt','kugou-diagnostic.json',
                     '_internal/settings.ini','_internal/__pycache__/data.pyc','_internal/Qt6VirtualKeyboard.dll']
            for name in allowed+private:
                path=root/name
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text('fixture',encoding='utf-8')
            self.assertEqual({p.relative_to(root).as_posix() for p in public_files(root)},set(allowed))
