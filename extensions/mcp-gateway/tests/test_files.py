import base64
import tempfile
import unittest

from files_store import Files


class Tests(unittest.TestCase):
    def test_roundtrip_and_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Files(tmp)
            data = f.validate("test.txt", base64.b64encode(b"hello").decode())
            fid = f.put("A", "test.txt", data, {"workspace_path": "inbound/test.txt"})
            self.assertEqual(f.get(fid, "A")["data"], b"hello")
            with self.assertRaises(ValueError):
                f.get(fid, "B")

    def test_bad_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Files(tmp)
            for name in ["../a.txt", "a.exe", "a/b.md"]:
                with self.assertRaises(ValueError):
                    f.validate(name, "aGVsbG8=")
            with self.assertRaises(ValueError):
                f.validate("a.txt", base64.b64encode(b"a" * 24001).decode())
            with self.assertRaises(ValueError):
                f.validate("a.txt", "%%")


if __name__ == "__main__":
    unittest.main(verbosity=2)
