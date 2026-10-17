"""Native lyric compatibility inference, lease recovery and channel isolation."""
import asyncio
import json
from pathlib import Path
import struct
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from kugou_native import (Layout, NativeLyricsReader, discover_layouts, dispatch_getter, member_getter,
                          read_msvc_string, read_object)
from kugou_reader import sample_channels
from kugou_windows import CommandPipe, NativeVisibility, PROPERTY


def getter(offset, prefix=b"\x8b\x81"):
    return prefix + struct.pack("<I", offset) + b"\xc3"


class BufferMemory:
    def __init__(self):
        self.data = {}
    def put(self, addr, data):
        self.data.update((addr + i, byte) for i, byte in enumerate(data))
    def read(self, addr, size):
        try:
            return bytes(self.data[addr + i] for i in range(size))
        except KeyError:
            return b""
    def pointer(self, addr):
        raw = self.read(addr, 8)
        return struct.unpack("<Q", raw)[0] if len(raw) == 8 else 0
    def string(self, addr, value):
        data = value.encode("utf-16le")
        if len(data) <= 14:
            self.put(addr, data.ljust(16, b"\0") + struct.pack("<QQ", len(data) // 2, 7))
        else:
            self.put(addr, struct.pack("<QQQQ", addr + 4096, 0, len(data) // 2, len(data) // 2))
            self.put(addr + 4096, data)


class FakePE:
    def __init__(self):
        self.FILE_HEADER = NS(Machine=0x8664)
        self.OPTIONAL_HEADER = NS(SizeOfImage=0x30000, ImageBase=0x180000000)
        self.bytes = bytearray(0x30000)
        exports = []
        def exported(name, rva, code):
            exports.append(NS(name=name.encode(), address=rva))
            self.bytes[rva:rva + len(code)] = code
        def dispatch(slot):
            return b"\x48\x8b\x89\x50\x08\0\0\x48\x8b\x01\x48\xff\x60" + bytes([slot])
        exported("?GetPlaybackPosition@LyricViewForPlaying@", 0x1100, dispatch(0x38))
        exported("?GetCurrentLineHighlinePercent@LyricViewForPlaying@", 0x1200, dispatch(0x50))
        exported("?GetLyricText@LyricViewForPlaying@", 0x1300,
                 b"\x48\x83\xec\x28\x48\x8b\x89\x50\x08\0\0\xe8" + struct.pack("<i", 0x1600 - 0x1310))
        self.bytes[0x1600:0x1607] = b"\x48\x8b\x41\x68\x48\x89\x02"
        for i, (name, offset) in enumerate((('Title', 0x70), ('Artist', 0x50), ('Album', 0xd0), ('Length', 0x140), ('Duration', 0x110))):
            exported("?Get" + name + "@LyricText@", 0x1700 + i * 0x40,
                     getter(offset, b"\x48\x8d\x81" if i < 3 else b"\x8b\x81"))
        self.bytes[0x2000:0x2007] = getter(0x188)
        float_code = getter(0x1f0, b"\xf3\x0f\x10\x81")
        self.bytes[0x2040:0x2040 + len(float_code)] = float_code
        struct.pack_into("<Q", self.bytes, 0x20100 + 0x38, self.OPTIONAL_HEADER.ImageBase + 0x2000)
        struct.pack_into("<Q", self.bytes, 0x20100 + 0x50, self.OPTIONAL_HEADER.ImageBase + 0x2040)
        self.DIRECTORY_ENTRY_EXPORT = NS(symbols=exports)
        self.sections = [NS(VirtualAddress=0x1000, Characteristics=0x60000020,
                            get_data=lambda: bytes(self.bytes[0x1000:0x10000])),
                         NS(VirtualAddress=0x20000, Characteristics=0x40000040,
                            get_data=lambda: bytes(self.bytes[0x20000:0x21000]))]
        self.closed = False
    def parse_data_directories(self, **_): pass
    def get_data(self, rva, size): return bytes(self.bytes[rva:rva + size])
    def close(self): self.closed = True


class NativeTests(unittest.TestCase):
    def test_layout_is_inferred_at_new_offsets_and_aslr_base(self):
        fake = FakePE()
        with patch("pefile.PE", return_value=fake):
            result = discover_layouts("new-ui.dll", 0x7fff90000000)
        self.assertEqual(result, [Layout(0x7fff90020100, 0x188, 0x68, 0x70, 0x50, 0xd0, 0x140, 0x110)])
        self.assertTrue(fake.closed)

    def test_unknown_or_changed_running_module_is_rejected(self):
        for mutation in ("export", "dispatch", "getter", "architecture", "running"):
            fake = FakePE()
            if mutation == "export": fake.DIRECTORY_ENTRY_EXPORT.symbols.pop()
            if mutation == "dispatch": fake.bytes[0x1100] = 0xcc
            if mutation == "getter": fake.bytes[0x2006] = 0x90
            if mutation == "architecture": fake.FILE_HEADER.Machine = 0x14c
            with self.subTest(mutation=mutation), patch("pefile.PE", return_value=fake), self.assertRaises(ValueError):
                discover_layouts("ui.dll", 0x7fff00000000, (lambda *_: b"bad") if mutation == "running" else None)
            self.assertTrue(fake.closed)

    def test_getters_reject_writes_truncation_and_arbitrary_code(self):
        self.assertEqual(member_getter(getter(0x188)), 0x188)
        self.assertEqual(member_getter(b"\x8b\x41\x40\xc3"), 0x40)
        for code in (getter(0x188)[:-1], b"\x89\x81\x88\x01\0\0\xc3", getter(20000), b"\xe9\0\0\0\0"):
            with self.subTest(code=code), self.assertRaises(ValueError):member_getter(code)
        with self.assertRaises(ValueError):dispatch_getter(b"\0" * 24)

    def test_native_metadata_handles_inline_heap_unicode_and_corruption(self):
        memory = BufferMemory()
        for text in ("Song", "你好", "Longer title with 😀"):
            memory.string(0x10000, text)
            self.assertEqual(read_msvc_string(memory.read, 0x10000), text)
        memory.put(0x10010, struct.pack("<QQ", 500000, 500000))
        with self.assertRaises(ValueError):read_msvc_string(memory.read, 0x10000)
        memory.string(0x20000, "bad\n")
        with self.assertRaises(ValueError):read_msvc_string(memory.read, 0x20000)

    def test_object_checks_document_identity_and_plausible_time(self):
        memory = BufferMemory(); obj, doc = 0x10000, 0x20000
        layout = Layout(0x7fff10000000, 0xc0, 0x78, 0x60, 0x40, 0xc0, 0x128, 0x100)
        memory.put(obj, struct.pack("<Q", layout.vtable));memory.put(obj + layout.lyric, struct.pack("<Q", doc))
        for name, value in (("title", "Song"), ("artist", "Singer"), ("album", "")):memory.string(doc + getattr(layout, name), value)
        memory.put(obj + layout.position, struct.pack("<i", 12345))
        memory.put(doc + layout.length, struct.pack("<i", 60000));memory.put(doc + layout.duration, struct.pack("<i", 0))
        self.assertEqual(read_object(memory, obj, layout)["position_ms"], 12345)
        memory.put(obj + layout.position, struct.pack("<i", -1))
        with self.assertRaises(ValueError):read_object(memory, obj, layout)
        memory.put(obj, struct.pack("<Q", 0))
        with self.assertRaises(ValueError):read_object(memory, obj, layout)

    def test_reader_rejects_other_song_and_freezes_stale_playing_data(self):
        tick = [10.0]; reader = NativeLyricsReader(lambda: tick[0])
        reader.process = NS(pid=42);reader.objects = [(100, None)];reader.retry_at = 100
        row = dict(title="Song", artist="Singer", album="", duration_ms=60000, position_ms=1000, document=200)
        media = dict(title="Song", artist="Singer", album="", playing=True, rate=1)
        with patch("kugou_native.read_object", return_value=row):
            self.assertEqual(reader.read(42, media)["mode"], "native")
            tick[0] += 2
            self.assertIsNone(reader.read(42, media))
            self.assertIsNone(reader.read(42, {**media, "title": "Different"}))
            self.assertIsNotNone(reader.read(42, {**media, "playing": False}))

    def test_newest_changing_view_wins_over_frozen_main_window(self):
        tick = [10.0];reader = NativeLyricsReader(lambda: tick[0])
        reader.process = NS(pid=42);reader.objects = [(100,None),(200,None)];reader.retry_at=100
        row = dict(title="Song", artist="Singer", album="", duration_ms=60000, document=300)
        media = dict(title="Song", artist="Singer", album="", playing=True)
        positions={100:1000,200:1000}
        with patch("kugou_native.read_object", side_effect=lambda _m,obj,_l:dict(row,position_ms=positions[obj])):
            reader.read(42,media);tick[0]+=.2;positions[200]=1200
            self.assertEqual(reader.read(42,media)["position_ms"],1200)

    def test_paused_cold_start_without_smtc_is_not_mistaken_for_playing(self):
        tick=[10.0];reader=NativeLyricsReader(lambda:tick[0]);reader.process=NS(pid=42)
        reader.objects=[(100,None)];reader.retry_at=100
        row=dict(title="Song",artist="Singer",album="",duration_ms=60000,position_ms=1000,document=300)
        with patch("kugou_native.read_object",return_value=row):
            self.assertFalse(reader.read(42)["playing"])
            tick[0]+=.2;self.assertFalse(reader.read(42)["playing"])
            row["position_ms"]+=200;tick[0]+=.2;self.assertTrue(reader.read(42)["playing"])

    def test_lease_pipe_expires_and_rejects_non_boolean_or_oversize_messages(self):
        tick=[10.0]
        pipe=CommandPipe.__new__(CommandPipe);pipe.clock=lambda:tick[0]
        pipe.pending=b"";pipe.replace_native=False;pipe.expires=0;pipe.quit=False
        pipe.feed(b'{"replace_native":tr');self.assertFalse(pipe.replace_native)
        pipe.feed(b'ue}\n');self.assertTrue(pipe.replace_native);self.assertEqual(pipe.expires,12)
        pipe.feed(b'{"replace_native":"false"}\n');self.assertTrue(pipe.replace_native)
        tick[0]=13;self.assertFalse(pipe.clock()<pipe.expires)
        pipe.feed(b"x"*9000);self.assertFalse(pipe.replace_native)
        pipe.feed(b'{"quit":true}\n');self.assertTrue(pipe.quit)

    def test_visibility_lease_preserves_closed_windows_and_recovers_dead_owner(self):
        class API:
            def __init__(self):self.owners={};self.visible={1:True,2:False,3:True}
            def GetPropW(self,h,_):return self.owners.get(h,0)
            def SetPropW(self,h,_,pid):self.owners[h]=pid;return True
            def RemovePropW(self,h,_):self.owners.pop(h,None)
            def IsWindowVisible(self,h):return self.visible[h]
            def ShowWindow(self,h,how):self.visible[h]=bool(how)
        api=API();win=NS(user=api,windows=lambda:[(1,42,True),(2,42,True),(3,42,False)],alive=lambda p:p==88)
        lease=NativeVisibility(win,owner_pid=99)
        self.assertEqual(lease.replace(42,True),1)
        self.assertFalse(api.visible[1]);self.assertFalse(api.visible[2]);self.assertTrue(api.visible[3])
        lease.close();self.assertTrue(api.visible[1]);self.assertFalse(api.visible[2])
        api.owners[1]=77;api.visible[1]=False;lease.close();self.assertTrue(api.visible[1])
        api.owners[1]=88;api.visible[1]=False;lease.close();self.assertFalse(api.visible[1])

    def test_native_channel_does_not_initialize_ocr_and_refuses_switch_race(self):
        media=dict(title="Song",artist="Singer",album="",duration_ms=0,playing=True)
        native=NS(read=Mock(return_value={**media,"duration_ms":60000,"position_ms":1234,"mode":"native"}))
        with patch("kugou_reader.smtc_snapshot",new=AsyncMock(side_effect=[media,media])), patch("kugou_reader.WindowReader") as ocr:
            result=asyncio.run(sample_channels(None,native,NS(pid=lambda:42)))
            self.assertEqual(result[0]["mode"],"native");ocr.assert_not_called()
        with patch("kugou_reader.smtc_snapshot",new=AsyncMock(side_effect=[media,{**media,"title":"Other"}])):
            self.assertIsNone(asyncio.run(sample_channels(None,native,NS(pid=lambda:42)))[0])

    def test_unknown_native_layout_falls_back_to_existing_hybrid_channel(self):
        media=dict(source="kugou",title="Song",artist="Singer",album="",duration_ms=0,playing=True,rate=1)
        window=NS(read=AsyncMock(return_value=(dict(duration_ms=60000,position_ms=1000),"ocr")))
        native=NS(read=Mock(side_effect=ValueError("unsupported layout")))
        with patch("kugou_reader.smtc_snapshot",new=AsyncMock(side_effect=[media,media])):
            packet,_,_=asyncio.run(sample_channels(None,native,NS(pid=lambda:42),window))
        self.assertEqual(packet["mode"],"hybrid")

    def test_replacement_requires_received_lyrics_and_visible_overlay(self):
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication
        from kugou import KugouPlayer, KugouSnapshot, parse_snapshot
        from test_kugou import Executor
        app=QApplication.instance() or QApplication([])
        tick=[10.0];player=KugouPlayer(clock=lambda:tick[0],executor=Executor(),autostart=False)
        original=player.process
        sent=[]
        process=NS(state=lambda:QProcess.ProcessState.Running,write=lambda data:sent.append(json.loads(data)))
        player.process=process
        try:
            player._snapshot=KugouSnapshot("Song","Singer","",60000,1000,True,mode="native")
            player.connected=True
            player._send_visibility();self.assertFalse(sent[-1]["replace_native"])
            player.lyrics_received=True;tick[0]+=.6;player._send_visibility()
            self.assertTrue(sent[-1]["replace_native"])
            player.set_overlay_visible(False);self.assertFalse(sent[-1]["replace_native"])
            player.set_overlay_visible(True);self.assertTrue(sent[-1]["replace_native"])
            player.configure_native_replacement(False);self.assertFalse(sent[-1]["replace_native"])
            packet=dict(source="kugou",title="Song",artist="Singer",album="",duration_ms=60000,
                        position_ms=1234,playing=True,mode="native")
            self.assertEqual(parse_snapshot(packet).mode,"native")
        finally:
            player.process=original;player.stop()

    def test_guides_describe_native_requirements_and_future_version_limits(self):
        root=Path(__file__).resolve().parents[1]
        for name in ("KUGOU.md","release/kugou-guide.txt"):
            text=(root/name).read_text(encoding="utf-8")
            for phrase in ("kugou.3","原生歌词","最小化","接替酷狗桌面歌词","歌词时间范围","不承诺"):
                self.assertIn(phrase,text)

    def test_failed_helper_restores_its_lease_even_after_qprocess_loses_pid(self):
        from PySide6.QtWidgets import QApplication
        from kugou import KugouPlayer
        from test_kugou import Executor
        app=QApplication.instance() or QApplication([])
        player=KugouPlayer(executor=Executor(),autostart=False)
        original=player.process;player.process=NS(processId=lambda:0)
        player._reader_pid=12345
        try:
            with patch("kugou_windows.NativeVisibility") as visibility:
                player._helper_failed()
                visibility.assert_called_once_with(owner_pid=12345)
                visibility.return_value.close.assert_called_once()
            self.assertFalse(player.connected)
        finally:
            player.process=original;player.stop()

    def test_real_evidence_separates_native_progress_from_audio_length(self):
        root=Path(__file__).resolve().parents[1]
        data=json.loads((root/"docs/research/kugou-native-20261017.json").read_text(encoding="utf-8"))
        self.assertEqual(data["version"],"0.8.0-beta.1-kugou.3")
        self.assertFalse(data["clean_machine"])
        self.assertTrue(all(data["packaged_native"]["checks"].values()))
        self.assertTrue(all(data["packaged_gui"]["checks"].values()))
        self.assertTrue(data["real_controls"]["pause_froze_native_position"])
        self.assertGreater(abs(data["real_controls"]["ui_seek_delta_ms"]),20000)
        self.assertFalse(data["real_controls"]["smtc_seek_changed_position"])
        self.assertIn("lyric_time_range",data["limitations"])
        self.assertFalse({"addresses","raw_memory","lyrics","token"} & data.keys())


if __name__ == "__main__":unittest.main()
