"""Deterministic identity helpers compatible with the Tracks macOS app.

The macOS app derives all persistent IDs from MD5 over UTF-8 strings:

    song    = "song|" + normalized(namespace) + "|" + NFC(relativePath)
    artist  = "artist|" + lowercase(NFC(name))
    album   = "album|" + artistID + "|" + lowercase(NFC(title))
    library = "library|" + canonicalPath

Swift's ``String.lowercased()`` performs context-free per-character mapping,
unlike Python's ``str.lower()`` which applies the Unicode final-sigma rule in
word contexts.  Lowercasing one scalar at a time reproduces Swift's result.
"""

import hashlib
import os
import unicodedata
import uuid


def normalize_nfc(value):
    return unicodedata.normalize("NFC", value)


def swift_lower(value):
    """Match Swift's locale-independent ``lowercased()``.

    Lowercasing one character at a time avoids Python's contextual final sigma
    mapping (Σ at the end of a word -> ς), which Swift does not apply.
    """

    return "".join(ch.lower() for ch in normalize_nfc(value))


def normalize_identifier(raw):
    return swift_lower((raw or "").strip())


def normalize_name(raw):
    return swift_lower(raw or "")


def normalize_relative_path(raw):
    return normalize_nfc(raw or "").lstrip("/")


def stable_uuid(text):
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    return str(uuid.UUID(hex=digest)).upper()


def song_uuid(namespace, relative_path):
    return stable_uuid(
        "song|" + normalize_identifier(namespace) + "|"
        + normalize_relative_path(relative_path)
    )


def artist_uuid(name):
    return stable_uuid("artist|" + normalize_name(name))


def album_uuid(artist_id, title):
    return stable_uuid("album|" + artist_id + "|" + normalize_name(title))


def library_uuid(path):
    return stable_uuid("library|" + path)


def canonical_path(path):
    return os.path.realpath(os.path.expanduser(path))


def is_under(path, root):
    if path == root:
        return True
    if root == "/":
        return path.startswith("/")
    return path.startswith(root.rstrip("/") + "/")


def relative_path(file_path, library_root):
    file_path = canonical_path(file_path)
    library_root = canonical_path(library_root)
    if not is_under(file_path, library_root):
        return None
    if file_path == library_root:
        return ""
    root = library_root.rstrip("/")
    prefix = "/" if root == "" else root + "/"
    return normalize_relative_path(file_path[len(prefix):])
