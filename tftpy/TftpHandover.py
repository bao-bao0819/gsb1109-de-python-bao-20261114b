# vim: ts=4 sw=4 et ai:
"""This module implements a recoverable handover journal for uploads received
by the TFTP server.

The problem it solves: the server may send the final ACK of an upload and then
the process (or the archiving pipeline behind it) restarts before the received
file is registered. Without a persistent record there is no way to tell a
fully received file apart from a half-written one that happens to sit on disk.

Every upload is tracked by a handover record that lives in a journal
directory and survives restarts. A record moves through these states:

    receiving  - the transfer is still in progress, data lands in a staging
                 file. Never trust the staging file's mere existence.
    staged     - the transfer completed, the content was hashed and the byte
                 count reconciled with the expected size (tsize). The file is
                 ready for the archiving process to register.
    registered - the archiving process moved the file to its final
                 destination and recorded the handover as done.
    incomplete - recovery found the record (or its file) to be partial or
                 corrupt. It must never be treated as a complete file.

If the last block of a transfer is retransmitted (the final ACK was lost),
the receiving side simply answers the transfer again and keeps using the
original handover. If a whole new upload arrives for a file name that already
has a staged or registered handover with the same content, the original
handover is reused; if the content differs, the new report is archived
separately under a unique destination.
"""


import hashlib
import json
import logging
import os
import time
import uuid

from .TftpShared import TftpException

log = logging.getLogger("tftpy.TftpHandover")

# Handover states.
RECEIVING = "receiving"
STAGED = "staged"
REGISTERED = "registered"
INCOMPLETE = "incomplete"

HASH_ALGORITHM = "sha256"
HASH_CHUNK_SIZE = 65536


def hash_file(path):
    """Compute the content hash of the file at the given path."""
    digest = hashlib.new(HASH_ALGORITHM)
    with open(path, "rb") as fobj:
        while True:
            chunk = fobj.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class TftpHandover:
    """A single handover record, persisted as JSON in the journal."""

    def __init__(self, handover_id, filename, journal):
        self.id = handover_id
        # The file name requested by the client, relative to the server root.
        self.filename = filename
        self.state = RECEIVING
        # Expected size as negotiated via the tsize option, None if unknown.
        self.expected_size = None
        # Bytes received so far.
        self.size = 0
        # Content hash, computed when the transfer completes.
        self.sha256 = None
        # Final destination, set when the handover is registered.
        self.destination = None
        # Session key (host:port) of the uploading client, informational.
        self.session = None
        self.created = time.time()
        self.updated = self.created
        self.journal = journal

    @property
    def staging_path(self):
        return self.journal.staging_path_for(self.id)

    def to_dict(self):
        return {
            "id": self.id,
            "filename": self.filename,
            "state": self.state,
            "expected_size": self.expected_size,
            "size": self.size,
            "sha256": self.sha256,
            "destination": self.destination,
            "session": self.session,
            "created": self.created,
            "updated": self.updated,
        }

    @classmethod
    def from_dict(cls, data, journal):
        handover = cls(data["id"], data["filename"], journal)
        handover.state = data["state"]
        handover.expected_size = data["expected_size"]
        handover.size = data["size"]
        handover.sha256 = data["sha256"]
        handover.destination = data["destination"]
        handover.session = data["session"]
        handover.created = data["created"]
        handover.updated = data["updated"]
        return handover

    def __str__(self):
        return "handover %s: %s (%s, %d bytes)" % (
            self.id, self.filename, self.state, self.size)


class TftpHandoverJournal:
    """A persistent journal of upload handovers on the receiving side.

    Records are individual JSON files written atomically, and received data
    is kept in staging files until the transfer completes, so a restart at
    any point leaves a consistent, inspectable state behind."""

    def __init__(self, path, root=None):
        self.path = os.path.abspath(path)
        # The server root that registered files are placed under.
        self.root = os.path.abspath(root) if root else None
        self.records_dir = os.path.join(self.path, "records")
        self.staging_dir = os.path.join(self.path, "staging")
        os.makedirs(self.records_dir, exist_ok=True)
        os.makedirs(self.staging_dir, exist_ok=True)

    def record_path(self, handover_id):
        return os.path.join(self.records_dir, handover_id + ".json")

    def staging_path_for(self, handover_id):
        return os.path.join(self.staging_dir, handover_id + ".part")

    def save(self, handover):
        """Persist a handover record atomically."""
        handover.updated = time.time()
        tmp_path = self.record_path(handover.id) + ".tmp"
        with open(tmp_path, "w") as fobj:
            json.dump(handover.to_dict(), fobj, indent=2, sort_keys=True)
            fobj.flush()
            os.fsync(fobj.fileno())
        os.replace(tmp_path, self.record_path(handover.id))

    def discard(self, handover):
        """Remove a handover record and any staging file it owns."""
        for path in (self.record_path(handover.id), handover.staging_path):
            if os.path.exists(path):
                os.unlink(path)

    def get(self, handover_id):
        """Load a handover record by id, or None if there is none."""
        path = self.record_path(handover_id)
        if not os.path.exists(path):
            return None
        with open(path) as fobj:
            return TftpHandover.from_dict(json.load(fobj), self)

    def entries(self, state=None):
        """Iterate over all handover records, optionally filtered by state."""
        for name in sorted(os.listdir(self.records_dir)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(self.records_dir, name)) as fobj:
                handover = TftpHandover.from_dict(json.load(fobj), self)
            if state is None or handover.state == state:
                yield handover

    def find(self, filename=None, states=None, sha256=None):
        """Find handovers matching the given criteria."""
        matches = []
        for handover in self.entries():
            if filename is not None and handover.filename != filename:
                continue
            if states is not None and handover.state not in states:
                continue
            if sha256 is not None and handover.sha256 != sha256:
                continue
            matches.append(handover)
        return matches

    def begin(self, filename, expected_size=None, session=None):
        """Open a new handover for an incoming upload, state receiving."""
        handover = TftpHandover(uuid.uuid4().hex, filename, self)
        handover.expected_size = expected_size
        handover.session = session
        self.save(handover)
        log.info("Opened handover %s for %s", handover.id, filename)
        return handover

    def note_progress(self, handover, size):
        """Record how many bytes of a transfer have been received so far."""
        handover.size = size
        self.save(handover)

    def complete(self, handover, fileobj=None):
        """Mark a handover as completely received (staged).

        The staging file is synced to disk, hashed and reconciled with the
        expected size before the record is marked staged. If another staged
        or registered handover already holds the same content for the same
        file name, the original handover is reused and this one discarded."""
        if fileobj is not None:
            fileobj.flush()
            os.fsync(fileobj.fileno())
        if not os.path.exists(handover.staging_path):
            raise TftpException(
                "Staging file missing for handover %s" % handover.id)
        handover.size = os.path.getsize(handover.staging_path)
        handover.sha256 = hash_file(handover.staging_path)
        if (handover.expected_size is not None
                and handover.size != handover.expected_size):
            log.error(
                "Handover %s: received %d bytes but expected %d",
                handover.id, handover.size, handover.expected_size)
            handover.state = INCOMPLETE
            self.save(handover)
            raise TftpException(
                "Received size %d does not match expected size %d"
                % (handover.size, handover.expected_size))
        # A retransmitted report with identical content reuses the original
        # handover instead of creating a duplicate archive entry.
        for existing in self.find(filename=handover.filename,
                                  states=(STAGED, REGISTERED),
                                  sha256=handover.sha256):
            if existing.id == handover.id:
                continue
            if existing.size != handover.size:
                continue
            log.info(
                "Handover %s duplicates %s for %s, reusing the original",
                handover.id, existing.id, handover.filename)
            self.discard(handover)
            return existing
        handover.state = STAGED
        self.save(handover)
        log.info("Handover %s for %s is complete and staged",
                 handover.id, handover.filename)
        return handover

    def register(self, handover, destination=None):
        """Register a staged handover with the archiving process.

        The staging file is moved atomically to its final destination and
        the record is marked registered. A same-named report with different
        content is archived separately under a unique destination name."""
        if handover.state == REGISTERED:
            log.debug("Handover %s is already registered", handover.id)
            return handover
        if handover.state != STAGED:
            raise TftpException(
                "Cannot register handover %s in state %s"
                % (handover.id, handover.state))
        if not self.verify(handover):
            handover.state = INCOMPLETE
            self.save(handover)
            raise TftpException(
                "Staged content for handover %s failed verification"
                % handover.id)
        if destination is None:
            if not self.root:
                raise TftpException(
                    "No destination given and the journal has no root")
            destination = os.path.join(
                self.root, handover.filename.lstrip("/"))
        destination = os.path.abspath(destination)
        if os.path.exists(destination):
            if hash_file(destination) == handover.sha256:
                log.info(
                    "Destination %s already holds this content", destination)
            else:
                # Same name but different content: archive separately.
                destination = self.unique_destination(
                    destination, handover.sha256)
        parent = os.path.dirname(destination)
        if parent:
            os.makedirs(parent, exist_ok=True)
        os.replace(handover.staging_path, destination)
        handover.destination = destination
        handover.state = REGISTERED
        self.save(handover)
        log.info("Handover %s registered at %s", handover.id, destination)
        return handover

    def unique_destination(self, destination, sha256):
        """Derive a destination name that does not clash with existing,
        different content at the same path."""
        candidate = "%s.%s" % (destination, sha256[:8])
        counter = 1
        while os.path.exists(candidate):
            if hash_file(candidate) == sha256:
                break
            candidate = "%s.%s.%d" % (destination, sha256[:8], counter)
            counter += 1
        return candidate

    def content_path(self, handover):
        """The path of the file holding this handover's content, if any."""
        if handover.state == REGISTERED:
            return handover.destination
        return handover.staging_path

    def verify(self, handover):
        """Reconcile a handover record with the actual file content and the
        expected size. Returns True only if the content on disk matches the
        record exactly."""
        path = self.content_path(handover)
        if not path or not os.path.exists(path):
            log.warning("Handover %s: no content at %s", handover.id, path)
            return False
        if os.path.getsize(path) != handover.size:
            log.warning("Handover %s: size mismatch at %s", handover.id, path)
            return False
        if (handover.expected_size is not None
                and handover.size != handover.expected_size):
            log.warning("Handover %s: does not match expected size",
                        handover.id)
            return False
        if handover.sha256 is None:
            log.warning("Handover %s: no content hash recorded", handover.id)
            return False
        if hash_file(path) != handover.sha256:
            log.warning("Handover %s: content hash mismatch at %s",
                        handover.id, path)
            return False
        return True

    def recover(self):
        """Reconcile the journal with disk after a restart.

        Records still marked receiving never completed and are demoted to
        incomplete, as are staged records whose content no longer verifies.
        A half-written file that happens to exist on disk is therefore never
        mistaken for a complete one. Returns a summary keyed by state."""
        summary = {RECEIVING: [], STAGED: [], REGISTERED: [],
                   INCOMPLETE: [], "corrupt": []}
        for handover in self.entries():
            if handover.state == RECEIVING:
                log.warning(
                    "Handover %s for %s was interrupted mid-transfer",
                    handover.id, handover.filename)
                handover.state = INCOMPLETE
                self.save(handover)
                summary[INCOMPLETE].append(handover)
            elif handover.state == STAGED:
                if self.verify(handover):
                    summary[STAGED].append(handover)
                else:
                    log.warning(
                        "Handover %s for %s failed verification",
                        handover.id, handover.filename)
                    handover.state = INCOMPLETE
                    self.save(handover)
                    summary[INCOMPLETE].append(handover)
            elif handover.state == REGISTERED:
                if self.verify(handover):
                    summary[REGISTERED].append(handover)
                else:
                    log.error(
                        "Registered handover %s for %s no longer verifies",
                        handover.id, handover.filename)
                    summary["corrupt"].append(handover)
            else:
                summary[INCOMPLETE].append(handover)
        # Staging files without a record are orphaned partials.
        known = {self.staging_path_for(handover.id)
                 for handover in self.entries()}
        for name in os.listdir(self.staging_dir):
            path = os.path.join(self.staging_dir, name)
            if path not in known:
                log.warning("Orphaned staging file %s has no handover", path)
                summary.setdefault("orphaned", []).append(path)
        return summary
