# vim: ts=4 sw=4 et ai:
"""Persistent identity records for uploaded files.

Instruments frequently upload every result under the same name (e.g.
``RESULT.DAT``) and may reboot in the middle of a transfer, restarting their
block numbering at block one. A plain receive directory cannot tell the
following situations apart:

* a brand new sampling round;
* a re-send of a transfer that already completed;
* the continuation of a transfer that was interrupted by a reboot.

This module keeps a ledger of *observations* keyed by boot code, sampling
round and content digests, and stores the received bytes in content
addressed blobs so that two rounds whose contents happen to be byte
identical still exist as two observations while sharing a single storage
entity (hard links).

Identity rules
--------------

* A new WRQ only becomes an observation once block one arrives, because the
  digest of the first block is part of the identity.
* A transfer may only be continued (resumed) when the boot code carried by
  the WRQ matches the boot code recorded for the incomplete observation and
  the re-sent prefix is byte identical to what is already stored.
* A completed observation is treated as a re-send only when boot code and
  first-block digest both match. The full stream is verified again at end of
  transfer.
* Without a boot code, same name plus an identical first block is never
  enough to splice a new transfer onto an old partial one. The transfer is
  accepted as a separate new observation instead, and the evidence that
  would be required to prove it belongs to the same round is exposed through
  :meth:`TftpReceiptStore.evidence_required_for_unverified`.
"""

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone

from .TftpShared import TftpException

log = logging.getLogger("tftpy.TftpReceipts")

RECEIPT_FILENAME = ".tftp-receipts.json"
BOOT_OPTION = "boot"
BLOB_DIRNAME = ".blobs"
PARTIAL_DIRNAME = ".partial"
OBSERVATION_DIRNAME = "observations"
LEDGER_VERSION = 1

DISPOSITION_NEW = "new-sample"
DISPOSITION_RESEND = "resend"
DISPOSITION_RESUME = "resume"

STATUS_PARTIAL = "partial"
STATUS_COMPLETE = "complete"


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


class TftpUploadReceipt:
    """File-like bookkeeping for one upload attempt.

    The real writable binary file is kept in :attr:`sink`; block writes are
    routed through :meth:`write_block` so that resume prefixes can be
    verified instead of being blindly appended.
    """

    def __init__(self, store, filename, full_path, boot, blksize, peer):
        self.store = store
        self.filename = filename
        self.full_path = full_path
        self.boot = boot
        self.blksize = blksize
        self.peer = peer
        self.record = None
        self.sink = None
        self.disposition = None
        self.hasher = hashlib.sha256()
        self.size = 0
        self.blocks_seen = 0
        self.finalized = False
        self._stored_blocks = 0

    @property
    def first_block_sha256(self):
        return self.record["first_block_sha256"] if self.record else None

    def write_block(self, blocknumber, data):
        """Verify and persist one DAT block.

        Block one activates the receipt: the store decides whether this is a
        new sample, a re-send or a resume. Resumed transfers re-send every
        block from block one, so the prefix already on disk is compared byte
        for byte before new blocks are appended.
        """
        if self.finalized:
            raise TftpException("write after finalized upload receipt")
        self.hasher.update(data)
        self.size += len(data)
        self.blocks_seen += 1

        if blocknumber == 1 and self.record is None:
            self.store._activate(self, data)

        if self.disposition == DISPOSITION_RESUME:
            self._write_resume_block(blocknumber, data)
        else:
            self.sink.write(data)

    def _write_resume_block(self, blocknumber, data):
        if blocknumber <= self._stored_blocks:
            offset = (blocknumber - 1) * self.blksize
            self.sink.seek(offset)
            existing = self.sink.read(len(data))
            if existing != data:
                raise TftpException(
                    "resume prefix mismatch at block %d; refusing to splice "
                    "transfers that are not byte identical" % blocknumber
                )
            return
        expected = self._stored_blocks + 1
        if blocknumber != expected:
            raise TftpException(
                "non contiguous block %d, expected %d"
                % (blocknumber, expected)
            )
        self.sink.seek(0, os.SEEK_END)
        self.sink.write(data)
        self._stored_blocks += 1
        self.record["blocks_received"] = self._stored_blocks

    def finalize(self):
        """Close the sink and publish or verify the observation."""
        if self.finalized:
            return
        self.finalized = True
        self.store._finalize(self)

    def abort(self):
        """Record an interrupted transfer, keeping its partial bytes."""
        if self.finalized:
            return
        self.finalized = True
        self.store._abort(self)


class TftpReceiptStore:
    """Persistent ledger of upload observations and their blobs."""

    def __init__(self, root, receipt_path=None):
        self.root = os.path.abspath(root)
        self.receipt_path = (
            os.path.abspath(receipt_path)
            if receipt_path
            else os.path.join(self.root, RECEIPT_FILENAME)
        )
        self.blob_root = os.path.join(self.root, BLOB_DIRNAME)
        self.partial_root = os.path.join(self.root, PARTIAL_DIRNAME)
        self.observation_root = os.path.join(self.root, OBSERVATION_DIRNAME)
        for directory in (
            self.root,
            self.blob_root,
            self.partial_root,
            self.observation_root,
        ):
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._active_record_ids = set()
        self._load()

    # ------------------------------------------------------------------
    # Ledger persistence
    # ------------------------------------------------------------------
    def _load(self):
        if os.path.exists(self.receipt_path):
            with open(self.receipt_path, "r", encoding="utf-8") as ledger:
                payload = json.load(ledger)
            self.records = payload.get("records", [])
        else:
            self.records = []

    def _save_locked(self):
        directory = os.path.dirname(self.receipt_path)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".receipts-", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as ledger:
                json.dump(
                    {"version": LEDGER_VERSION, "records": self.records},
                    ledger,
                    indent=2,
                    sort_keys=True,
                )
                ledger.flush()
                os.fsync(ledger.fileno())
            os.replace(tmp_path, self.receipt_path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    # ------------------------------------------------------------------
    # Transfer lifecycle
    # ------------------------------------------------------------------
    def begin_upload(self, filename, full_path, boot, blksize, peer):
        """Create a pending receipt before block one is known."""
        return TftpUploadReceipt(
            self,
            filename,
            full_path,
            self._normalize_boot(boot),
            int(blksize),
            peer,
        )

    @staticmethod
    def _normalize_boot(boot):
        if boot is None:
            return None
        boot = str(boot).strip()
        return boot or None

    def _activate(self, handle, first_block):
        with self._lock:
            first_hash = hashlib.sha256(first_block).hexdigest()
            now = _utcnow()

            resume_record = self._find_resume_candidate(
                handle, first_hash
            )
            if resume_record is not None:
                self._open_resume(handle, resume_record, now)
                return

            resend_record = self._find_resend_candidate(
                handle, first_hash
            )
            if resend_record is not None:
                self._open_resend(handle, resend_record, now)
                return

            self._open_new_sample(handle, first_hash, now)

    def _find_resume_candidate(self, handle, first_hash):
        if handle.boot is None:
            return None
        for record in reversed(self.records):
            if (
                record["status"] == STATUS_PARTIAL
                and record["filename"] == handle.filename
                and record["boot"] == handle.boot
                and record["blksize"] == handle.blksize
                and record["first_block_sha256"] == first_hash
            ):
                if record["id"] in self._active_record_ids:
                    raise TftpException(
                        "partial observation %s already has an active "
                        "transfer" % record["id"]
                    )
                return record
        return None

    def _find_resend_candidate(self, handle, first_hash):
        if handle.boot is None:
            return None
        for record in reversed(self.records):
            if (
                record["status"] == STATUS_COMPLETE
                and record["filename"] == handle.filename
                and record["boot"] == handle.boot
                and record["first_block_sha256"] == first_hash
            ):
                return record
        return None

    def _open_new_sample(self, handle, first_hash, now):
        record_id = uuid.uuid4().hex
        round_number = self._next_round(handle.filename)
        partial_rel = os.path.join(
            PARTIAL_DIRNAME, "%s.part" % record_id
        )
        record = {
            "id": record_id,
            "filename": handle.filename,
            "boot": handle.boot,
            "round": round_number,
            "status": STATUS_PARTIAL,
            "blksize": handle.blksize,
            "first_block_sha256": first_hash,
            "sha256": None,
            "size": None,
            "blocks_received": 0,
            "blob": None,
            "observation": None,
            "partial": partial_rel,
            "peer": handle.peer,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "attempts": [
                self._attempt(handle, now, DISPOSITION_NEW)
            ],
        }
        self.records.append(record)
        handle.record = record
        handle.disposition = DISPOSITION_NEW
        handle.sink = self._open_partial_sink(partial_rel, append=False)
        self._active_record_ids.add(record_id)
        self._save_locked()

    def _open_resume(self, handle, record, now):
        partial_rel = record["partial"]
        partial_path = os.path.join(self.root, partial_rel)
        if not os.path.exists(partial_path):
            raise TftpException(
                "partial bytes for observation %s are missing" % record["id"]
            )
        sink = open(partial_path, "r+b")
        actual_size = os.fstat(sink.fileno()).st_size
        blksize = record["blksize"]
        if actual_size % blksize != 0:
            sink.close()
            raise TftpException(
                "partial file size %d is not a multiple of blksize %d"
                % (actual_size, blksize)
            )
        # The bytes on disk are the source of truth: a hard crash may have
        # flushed a block before the ledger was saved, leaving the ledger
        # count behind. A smaller file means data was lost externally.
        disk_blocks = actual_size // blksize
        if disk_blocks < record["blocks_received"]:
            sink.close()
            raise TftpException(
                "partial file is missing blocks recorded in the ledger "
                "(%d on disk vs %d recorded)"
                % (disk_blocks, record["blocks_received"])
            )
        if disk_blocks != record["blocks_received"]:
            log.warning(
                "ledger for observation %s lags the partial file "
                "(%d vs %d blocks); trusting the bytes on disk",
                record["id"], record["blocks_received"], disk_blocks,
            )
            record["blocks_received"] = disk_blocks
        record["status"] = STATUS_PARTIAL
        record["attempts"].append(
            self._attempt(handle, now, DISPOSITION_RESUME)
        )
        record["updated_at"] = now
        record["peer"] = handle.peer
        handle.record = record
        handle.disposition = DISPOSITION_RESUME
        handle.sink = sink
        handle._stored_blocks = disk_blocks
        self._active_record_ids.add(record["id"])
        self._save_locked()

    def _open_resend(self, handle, record, now):
        attempt_number = len(record["attempts"]) + 1
        verify_rel = os.path.join(
            PARTIAL_DIRNAME,
            "%s-verify-%d.part" % (record["id"], attempt_number),
        )
        record["attempts"].append(
            self._attempt(handle, now, DISPOSITION_RESEND)
        )
        record["updated_at"] = now
        record["peer"] = handle.peer
        handle.record = record
        handle.disposition = DISPOSITION_RESEND
        handle.sink = self._open_partial_sink(verify_rel, append=False)
        self._save_locked()

    def _next_round(self, filename):
        rounds = [
            record["round"]
            for record in self.records
            if record["filename"] == filename
        ]
        return (max(rounds) + 1) if rounds else 1

    @staticmethod
    def _attempt(handle, now, disposition):
        return {
            "disposition": disposition,
            "peer": handle.peer,
            "started_at": now,
            "ended_at": None,
            "result": None,
        }

    def _open_partial_sink(self, relative_path, append):
        mode = "a+b" if append else "wb"
        path = os.path.join(self.root, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return open(path, mode)

    # ------------------------------------------------------------------
    # Finalization / abort
    # ------------------------------------------------------------------
    def _finalize(self, handle):
        with self._lock:
            now = _utcnow()
            sink = handle.sink
            sink.flush()
            os.fsync(sink.fileno())
            sink.close()
            digest = handle.hasher.hexdigest()
            record = handle.record
            record["updated_at"] = now
            record["attempts"][-1]["ended_at"] = now

            try:
                if handle.disposition == DISPOSITION_RESEND:
                    self._finalize_resend(handle, digest, now)
                else:
                    self._finalize_observation(handle, digest, now)
            finally:
                # Even a failed verification must release the claim so the
                # observation can be retried or resumed later.
                self._active_record_ids.discard(record["id"])
                self._save_locked()

    def _finalize_resend(self, handle, digest, now):
        record = handle.record
        sink_path = handle.sink.name
        attempt = record["attempts"][-1]
        if digest != record["sha256"] or handle.size != record["size"]:
            attempt["result"] = "content-mismatch"
            record["attempts"][-1]["ended_at"] = now
            self._save_locked()
            raise TftpException(
                "re-sent content does not match observation %s "
                "(digest %s != %s); bytes retained at %s"
                % (record["id"], digest, record["sha256"], sink_path)
            )
        attempt["result"] = "verified"
        if os.path.exists(sink_path):
            os.unlink(sink_path)

    def _finalize_observation(self, handle, digest, now):
        record = handle.record
        sink_path = handle.sink.name
        blob_rel = os.path.join(
            BLOB_DIRNAME, digest[:2], digest
        )
        blob_path = os.path.join(self.root, blob_rel)
        if not os.path.exists(blob_path):
            os.makedirs(os.path.dirname(blob_path), exist_ok=True)
            os.replace(sink_path, blob_path)
        elif os.path.exists(sink_path):
            os.unlink(sink_path)

        observation_rel = os.path.join(
            OBSERVATION_DIRNAME,
            "round-%04d-%s" % (record["round"], record["id"]),
            os.path.basename(handle.full_path),
        )
        observation_path = os.path.join(self.root, observation_rel)
        os.makedirs(os.path.dirname(observation_path), exist_ok=True)
        self._link_blob(blob_path, observation_path)

        record.update(
            {
                "status": STATUS_COMPLETE,
                "sha256": digest,
                "size": handle.size,
                "blocks_received": handle.blocks_seen
                if handle.disposition == DISPOSITION_NEW
                else handle._stored_blocks,
                "blob": blob_rel,
                "observation": observation_rel,
                "partial": None,
                "completed_at": now,
            }
        )
        record["attempts"][-1]["result"] = "complete"

    @staticmethod
    def _link_blob(blob_path, observation_path):
        if os.path.exists(observation_path):
            os.unlink(observation_path)
        try:
            os.link(blob_path, observation_path)
        except OSError:
            shutil.copy2(blob_path, observation_path)

    def _abort(self, handle):
        with self._lock:
            now = _utcnow()
            if handle.sink is not None and not handle.sink.closed:
                handle.sink.flush()
                handle.sink.close()
            record = handle.record
            if record is None:
                return
            record["updated_at"] = now
            record["attempts"][-1]["ended_at"] = now
            self._active_record_ids.discard(record["id"])
            if handle.disposition == DISPOSITION_RESEND:
                record["attempts"][-1]["result"] = "aborted"
                sink_path = handle.sink.name
                if os.path.exists(sink_path):
                    os.unlink(sink_path)
            else:
                record["attempts"][-1]["result"] = "interrupted"
                record["status"] = STATUS_PARTIAL
                record["blocks_received"] = (
                    handle._stored_blocks
                    if handle.disposition == DISPOSITION_RESUME
                    else handle.blocks_seen
                )
            self._save_locked()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def observations(self, filename=None):
        """Return ledger records, optionally filtered by filename."""
        with self._lock:
            if filename is None:
                return [dict(record) for record in self.records]
            return [
                dict(record)
                for record in self.records
                if record["filename"] == filename
            ]

    def evidence_required_for_unverified(self):
        """List evidence missing whenever a boot-less WRQ meets a partial.

        The first section is the catalog of evidence that can prove two
        transfers belong to the same sampling round; the second section
        describes each open partial observation that currently cannot be
        joined automatically.
        """
        with self._lock:
            catalog = [
                {
                    "evidence": "启动代号（boot code）或等价的会话令牌",
                    "evidence_key": "boot",
                    "why": "区分仪器重启后的同任务续传与新一轮采样；"
                           "须通过 WRQ 选项 %r 携带且与半份记录一致"
                    % BOOT_OPTION,
                    "how": "仪器在 WRQ 选项中附带不变的 boot 值，"
                           "服务器与台账中的 boot 字段比对",
                },
                {
                    "evidence": "采集轮次号（round）",
                    "evidence_key": "round",
                    "why": "证明目标是同一观测轮次而非下一次采样，"
                           "轮次号在台账内按文件名单调递增",
                    "how": "由服务器台账分配并在记录中回显，"
                           "续传请求须定位到该 round 的记录",
                },
                {
                    "evidence": "块编号与偏移连续性",
                    "evidence_key": "block-continuity",
                    "why": "重启后仪器从一号块重发，服务器需要期望块号"
                           "（blocks_received + 1）与一致的 blksize",
                    "how": "比对重发的块序号、块大小与台账中的块计数，"
                           "新块必须恰好接在已收前缀之后",
                },
                {
                    "evidence": "首块及已收前缀的内容摘要（sha256）",
                    "evidence_key": "prefix-digest-match",
                    "why": "同名和首块一致本身不充分；前缀必须逐字节相同，"
                           "才能排除恰有相同开头的不同采样",
                    "how": "逐块重算 sha256 并与 first_block_sha256、"
                           "半份文件偏移处的字节比对",
                },
                {
                    "evidence": "会话与网络关联证据",
                    "evidence_key": "session-correlation",
                    "why": "同一仪器的 IP/TID、时间戳落在重试窗口内可"
                           "辅助确认两次传输的关联性",
                    "how": "核对 peer 地址、原会话时间戳与本次 WRQ 的"
                           "时间间隔及会话标识",
                },
                {
                    "evidence": "仪器侧重启与任务日志",
                    "evidence_key": "instrument-log",
                    "why": "仪器日志可证明重启后重发的是中断前的同一"
                           "任务，而不是新任务；tsize 应与总长度吻合",
                    "how": "调取仪器重启记录、传输任务日志及 WRQ tsize"
                           "选项进行人工或离线核验",
                },
            ]
            open_partials = []
            for record in self.records:
                if record["status"] != STATUS_PARTIAL:
                    continue
                # The report is framed around an incoming transfer carrying no
                # boot code. Presenting a matching boot value (or proving the
                # stored round another way) is therefore always outstanding.
                # When the stored partial itself has no boot, no automatic
                # match is ever possible and manual correlation is required.
                missing = [
                    "boot",
                    "round-confirmation",
                    "block-continuity",
                    "prefix-digest-match",
                    "session-correlation",
                ]
                open_partials.append(
                    {
                        "record_id": record["id"],
                        "filename": record["filename"],
                        "round": record["round"],
                        "boot": record["boot"],
                        "match_possible": record["boot"] is not None,
                        "blksize": record["blksize"],
                        "blocks_received": record["blocks_received"],
                        "next_expected_block": record["blocks_received"] + 1,
                        "first_block_sha256": record[
                            "first_block_sha256"
                        ],
                        "updated_at": record["updated_at"],
                        "missing_evidence": missing,
                    }
                )
            return {"catalog": catalog, "open_partials": open_partials}
