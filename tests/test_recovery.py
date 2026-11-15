import hashlib
import json
import os
import tempfile
import unittest

from tftpy import TftpRecovery as recovery
from tftpy.TftpRecovery import (
    IncompleteRecoveryError,
    Replica,
    RecoveryDeclarationError,
    SegmentDigest,
    VersionDeclaration,
    recover_batch,
    recover_file,
)


BS = 8


def payload(length):
    return bytes((index * 7 + 3) % 256 for index in range(length))


def block_map(data, blocksize=BS, missing=(), corrupt=None):
    """Split *data* into TFTP blocks, including the trailing empty block."""
    full, remainder = divmod(len(data), blocksize)
    blocks = [data[i * blocksize:(i + 1) * blocksize] for i in range(full)]
    blocks.append(data[full * blocksize:] if remainder else b"")
    result = {}
    for number, block in enumerate(blocks, start=1):
        if number in missing:
            continue
        if corrupt and number in corrupt:
            block = corrupt[number]
        result[number] = block
    return result


def replica(name, data, **kwargs):
    return Replica(name, block_map(data, **kwargs))


def digest(start, end, data, algorithm="sha256"):
    return SegmentDigest(start, end, hashlib.new(algorithm, data).hexdigest(), algorithm)


class TftpRecoveryTest(unittest.TestCase):
    def test_blocks_are_combined_across_replicas(self):
        data = payload(24)
        first = replica("first", data, missing={2})
        second = replica("second", data, missing={3})
        result = recover_file([first, second], name="sample.bin")
        self.assertTrue(result.complete)
        self.assertEqual(result.data, data)
        self.assertEqual(result.legal_completions, 1)
        self.assertEqual(result.blocks[1].sources, ("second",))
        self.assertEqual(result.blocks[2].sources, ("first",))
        self.assertEqual(result.blocks[0].sources, ("first", "second"))

    def test_digest_spanning_two_blocks_is_checked(self):
        data = payload(48)
        whole = digest(0, 32, data[:32])
        declaration = VersionDeclaration(
            size=len(data), blocksize=BS, version="v1", digests=(whole,)
        )
        first = replica("first", data, missing={2})
        second = replica("second", data, missing={3})
        result = recover_file([first, second], declaration, name="a.bin")
        self.assertTrue(result.complete)
        self.assertEqual(result.data, data)
        segment = result.segments[0]
        self.assertTrue(segment.verified)
        # Each replica is missing a different block inside the segment, so
        # readback is split by the usable replica set.
        self.assertEqual(segment.sources, ())
        self.assertEqual(
            [(item.start, item.end, item.sources) for item in segment.readback],
            [
                (0, 8, ("first", "second")),
                (8, 16, ("second",)),
                (16, 24, ("first",)),
                (24, 32, ("first", "second")),
            ],
        )

    def test_digest_segment_with_single_readback_source(self):
        data = payload(32)
        whole = digest(0, 32, data)
        declaration = VersionDeclaration(
            size=32, blocksize=BS, digests=(whole,)
        )
        first = replica("first", data, missing={7})
        second = replica("second", data, missing={2})
        result = recover_file([first, second], declaration, name="a2.bin")
        self.assertTrue(result.complete)
        self.assertEqual(result.segments[0].sources, ("first",))
        self.assertEqual(result.segments[0].readback, ())

    def test_digest_can_start_and_end_inside_blocks(self):
        data = payload(30)
        inner = digest(5, 19, data[5:19])
        declaration = VersionDeclaration(size=30, blocksize=BS, digests=(inner,))
        result = recover_file(
            [Replica("only", block_map(data))], declaration, name="b.bin"
        )
        self.assertTrue(result.complete)
        self.assertTrue(result.segments[0].verified)

    def test_two_blocks_wrong_in_different_replicas(self):
        data = payload(40)
        bad_one = replica("one", data, corrupt={2: b"X" * BS})
        bad_two = replica("two", data, corrupt={5: b"Y" * BS})
        good = replica("three", data)
        declaration = VersionDeclaration(
            size=len(data), blocksize=BS, digests=(digest(0, 40, data),)
        )
        result = recover_file(
            [bad_one, bad_two, good], declaration, name="c.bin"
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.data, data)

    def test_block_vote_is_not_enough_for_two_simultaneous_errors(self):
        data = payload(16)
        # Per-block majority is wrong in both blocks, because the third
        # replica agrees with a different wrong value in each block.  Only the
        # cross-block digest identifies the correct combination.
        first = replica("first", data, corrupt={2: b"2" * BS})
        second = replica("second", data, corrupt={1: b"1" * BS})
        third = replica("third", data, corrupt={1: b"X" * BS, 2: b"Y" * BS})
        declaration = VersionDeclaration(
            size=len(data),
            blocksize=BS,
            digests=(digest(0, 16, data),),
        )
        result = recover_file(
            [first, second, third], declaration, name="d.bin"
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.data, data)
        self.assertEqual(result.legal_completions, 1)

    def test_two_legal_completions_deliver_only_shared_bytes(self):
        data = payload(32)
        first = replica("first", data, corrupt={2: b"A" * BS})
        second = replica("second", data, corrupt={2: b"B" * BS})
        declaration = VersionDeclaration(
            size=32, blocksize=BS, digests=(digest(0, 8, data[:8]),)
        )
        result = recover_file([first, second], declaration, name="e.bin")
        self.assertFalse(result.complete)
        self.assertEqual(result.legal_completions, 2)
        self.assertEqual(result.reason, recovery.REASON_AMBIGUOUS)
        self.assertEqual(
            [(item.start, item.end) for item in result.pending_ranges],
            [(8, 16)],
        )
        self.assertEqual(
            [item.data if item.complete else item.sources]
            if False
            else [(item.start, item.end, item.sources) for item in result.ranges],
            [(0, 8, ("first", "second")), (8, 16, ()), (16, 32, ("first", "second"))],
        )

    def test_disjoint_legal_completions_have_no_deliverable_bytes(self):
        first = Replica("first", {1: b"A" * BS, 2: b""})
        second = Replica("second", {1: b"B" * BS, 2: b""})
        result = recover_file(
            [first, second], VersionDeclaration(size=BS, blocksize=BS),
            name="f.bin",
        )
        self.assertFalse(result.complete)
        self.assertEqual(result.legal_completions, 2)
        self.assertEqual(result.recovered_ranges, ())
        self.assertEqual(
            [(item.start, item.end) for item in result.pending_ranges],
            [(0, BS)],
        )

    def test_final_short_block_keeps_its_source(self):
        data = payload(21)
        tail = Replica("tail", {1: data[:8], 3: data[16:21]})
        middle = Replica("middle", {2: data[8:16]})
        declaration = VersionDeclaration(size=21, blocksize=BS, version="r2")
        result = recover_file([tail, middle], declaration, name="g.bin")
        self.assertTrue(result.complete)
        self.assertEqual(result.data, data)
        last = result.last_block
        self.assertEqual((last.block, last.length), (3, 5))
        self.assertTrue(last.is_short)
        self.assertEqual(last.sources, ("tail",))
        self.assertEqual(result.eof_source, ("tail",))

    def test_exact_multiple_requires_and_attributes_empty_block(self):
        data = payload(16)
        declaration = VersionDeclaration(size=16, blocksize=BS)
        without_tail = Replica("without", {1: data[:8], 2: data[8:]})
        incomplete = recover_file([without_tail], declaration, name="h.bin")
        self.assertFalse(incomplete.complete)
        self.assertEqual(incomplete.reason, recovery.REASON_MISSING)

        with_tail = Replica("with", block_map(data))
        result = recover_file([with_tail], declaration, name="h.bin")
        self.assertTrue(result.complete)
        last = result.last_block
        self.assertEqual((last.block, last.length), (3, 0))
        self.assertTrue(last.is_empty)
        self.assertEqual(result.eof_source, ("with",))

    def test_confirmed_empty_file_has_a_source(self):
        empty = Replica("empty", {1: b""})
        result = recover_file(
            [empty], VersionDeclaration(size=0, blocksize=BS), name="i.bin"
        )
        self.assertTrue(result.complete)
        self.assertTrue(result.empty)
        self.assertEqual(result.data, b"")
        self.assertEqual(result.empty_sources, ("empty",))
        self.assertEqual(result.last_block.sources, ("empty",))

    def test_empty_file_needs_an_empty_block(self):
        result = recover_file(
            [Replica("nothing", {})],
            VersionDeclaration(size=0, blocksize=BS),
            name="j.bin",
        )
        self.assertFalse(result.complete)
        self.assertFalse(result.empty)

    def test_extra_blocks_beyond_declared_size_are_ignored(self):
        data = payload(16)
        blocks = block_map(data)
        blocks[4] = b"stale!!!"
        result = recover_file(
            [Replica("stale", blocks)],
            VersionDeclaration(size=16, blocksize=BS),
            name="k.bin",
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.data, data)

    def test_wrong_length_block_is_not_used(self):
        data = payload(24)
        broken = Replica(
            "broken", {1: data[:8], 2: data[8:15], 3: data[16:24], 4: b""}
        )
        result = recover_file(
            [broken], VersionDeclaration(size=24, blocksize=BS), name="l.bin"
        )
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, recovery.REASON_MISSING)

    def test_replica_with_other_version_is_not_used(self):
        data = payload(8)
        old = Replica("old", {1: data, 2: b""}, version="old")
        new = Replica("new", {1: data, 2: b""}, version="new")
        declaration = VersionDeclaration(
            size=8, blocksize=BS, version="new"
        )
        result = recover_file([old, new], declaration, name="m.bin")
        self.assertTrue(result.complete)
        self.assertEqual(result.blocks[0].sources, ("new",))

        only_old = recover_file([old], declaration, name="m.bin")
        self.assertFalse(only_old.complete)
        self.assertEqual(only_old.reason, recovery.REASON_MISSING)

    def test_replica_with_other_declared_size_is_not_used(self):
        data = payload(8)
        wrong = Replica("wrong", {1: data, 2: b""}, declared_size=9)
        result = recover_file(
            [wrong], VersionDeclaration(size=8, blocksize=BS), name="n.bin"
        )
        self.assertFalse(result.complete)

    def test_unresolved_digest_means_no_completion(self):
        data = payload(16)
        declaration = VersionDeclaration(
            size=16,
            blocksize=BS,
            digests=(digest(0, 16, payload(16)[::-1]),),
        )
        result = recover_file(
            [Replica("only", block_map(data))], declaration, name="o.bin"
        )
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, recovery.REASON_NO_COMPLETION)

    def test_digest_outside_declared_size_is_rejected(self):
        with self.assertRaises(RecoveryDeclarationError):
            recover_file(
                [Replica("only", {1: payload(8), 2: b""})],
                VersionDeclaration(
                    size=8, blocksize=BS, digests=(digest(0, 9, b"x" * 9),)
                ),
                name="p.bin",
            )

    def test_missing_short_block_leaves_size_unknown(self):
        data = payload(16)
        result = recover_file(
            [Replica("only", {1: data[:8], 2: data[8:]})],
            VersionDeclaration(blocksize=BS),
            name="q.bin",
        )
        self.assertFalse(result.complete)
        self.assertTrue(result.eof_marker_missing)
        self.assertEqual(result.reason, recovery.REASON_MISSING)

    def test_size_is_inferred_from_short_block_when_not_declared(self):
        data = payload(21)
        result = recover_file(
            [Replica("only", block_map(data))],
            VersionDeclaration(blocksize=BS),
            name="r.bin",
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.data, data)
        self.assertTrue(result.size_inferred)

    def test_blocksize_is_inferred_from_full_blocks(self):
        data = payload(40)
        result = recover_file(
            [Replica("only", block_map(data))],
            VersionDeclaration(size=40),
            name="s.bin",
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.blocksize, BS)

    def test_conflicting_replica_blocksizes_are_rejected(self):
        with self.assertRaises(RecoveryDeclarationError):
            recover_file(
                [
                    Replica("a", {1: b"12345678"}, blocksize=8),
                    Replica("b", {1: b"1234567890123456"}, blocksize=16),
                ],
                name="t.bin",
            )

    def test_search_limit_still_delivers_agreed_blocks(self):
        data = payload(32)
        declaration = VersionDeclaration(
            size=32,
            blocksize=BS,
            digests=(digest(0, 8, data[:8]), digest(8, 16, data[8:16])),
            max_candidates=2,
            max_nodes=2,
        )
        replicas = [
            replica("a", data, corrupt={4: b"A" * BS}),
            replica("b", data, corrupt={4: b"B" * BS}),
            replica("c", data, corrupt={4: b"C" * BS}),
        ]
        result = recover_file(replicas, declaration, name="u.bin")
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, recovery.REASON_LIMIT)
        self.assertEqual(
            [(item.start, item.end) for item in result.recovered_ranges],
            [(0, 24), (32, 32)][:1],
        )
        self.assertEqual(
            [(item.start, item.end) for item in result.pending_ranges],
            [(24, 32)],
        )

    def test_json_manifest_batch_round_trip(self):
        data = payload(24)
        encoded = {
            str(number): "hex:" + block.hex()
            for number, block in block_map(data).items()
        }
        manifest = {
            "files": [
                {
                    "name": "one.bin",
                    "replicas": [
                        {
                            "name": "a",
                            "blocks": {
                                "1": encoded["1"],
                                "3": encoded["3"],
                            },
                        },
                        {"name": "b", "blocks": {"2": encoded["2"],
                                                 "4": encoded["4"]}},
                    ],
                    "size": 24,
                    "blocksize": 8,
                },
                {
                    "filename": "two.bin",
                    "replicas": [{"name": "c", "blocks": {"1": ""}}],
                    "size": 0,
                    "blocksize": 8,
                },
            ]
        }
        result = recover_batch(manifest["files"])
        self.assertEqual(len(result), 2)
        self.assertTrue(result.complete)
        self.assertEqual(result["one.bin"].data, data)
        self.assertEqual(result["two.bin"].empty_sources, ("c",))
        self.assertEqual(
            [item.name for item in result.confirmed_empty_files], ["two.bin"]
        )

    def test_batch_write_all_writes_empty_file(self):
        with tempfile.TemporaryDirectory() as directory:
            result = recover_batch(
                [
                    {
                        "name": "empty.bin",
                        "replicas": [{"name": "e", "blocks": {"1": ""}}],
                        "size": 0,
                        "blocksize": 8,
                    }
                ]
            )
            written = result.write_all(directory)
            path = os.path.join(directory, "empty.bin")
            self.assertEqual(written, {"empty.bin": path})
            self.assertTrue(os.path.exists(path))
            self.assertEqual(os.path.getsize(path), 0)

    def test_incomplete_write_raises(self):
        result = recover_file(
            [Replica("a", {1: b"A" * BS})],
            VersionDeclaration(size=2 * BS, blocksize=BS),
            name="v.bin",
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(IncompleteRecoveryError):
                result.write(os.path.join(directory, "v.bin"))

    def test_iterator_and_dict_view(self):
        data = payload(8)
        result = recover_file(
            [Replica("only", block_map(data))],
            VersionDeclaration(size=8, blocksize=BS),
            name="w.bin",
        )
        self.assertEqual(list(result.iter_delivered()), [(0, data)])
        view = result.as_dict()
        self.assertTrue(view["complete"])
        json.dumps(view)
        self.assertEqual(view["ranges"][0]["status"], recovery.RECOVERED)


if __name__ == "__main__":
    unittest.main()
