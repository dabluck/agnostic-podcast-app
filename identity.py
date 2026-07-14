#!/usr/bin/env python3
"""Shared identity/normalization helpers for the ingestion pipeline (see DEDUP_RULES.md).

Every identity-affecting rule lives here exactly once: enclosure-URL
normalization, title normalization, private-feed detection, epoch-unit
detection, and date parsing. Ingesters and pipeline scripts import from this
module; tests_identity.py covers it.
"""

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# --- enclosure URLs ---------------------------------------------------------
# An enclosure's stable identity is the final host/path of the media file:
# query strings rotate (?updated=, tokens) and ad/tracking redirectors chain
# prefixes (podtrac -> swap.fm -> pscrb.fm -> real host).
MEDIA_TAIL = re.compile(
    r"([a-z0-9.-]+\.[a-z]{2,}/[^ ]*\.(?:mp3|m4a|aac|ogg|wav|mp4|mp4a))$", re.I
)
# A hostname embedded in the path (must be followed by '/', so a trailing
# "file.mp3" never counts as a host).
EMBEDDED_HOST = re.compile(r"(?:^|/)((?:[a-z0-9-]+\.)+[a-z]{2,})(?::\d+)?(?=/)", re.I)


def core_url(u):
    """Reduce an enclosure URL to its stable tail: the media path from the
    LAST embedded hostname onward, lowercased. This is what actually strips
    redirector chains (podtrac -> swap.fm -> pscrb.fm -> real host); matching
    from the first host keeps the whole chain and defeats cross-app matching,
    which is exactly the bug tests_identity.py guards against."""
    u = (u or "").split("?")[0]
    last = None
    for last in EMBEDDED_HOST.finditer(u):
        pass
    if last:
        tail = u[last.start(1):]
        if MEDIA_TAIL.search(tail):
            return tail.lower()
    m = MEDIA_TAIL.search(u)
    return m.group(1).lower() if m else u.lower()


# --- titles -----------------------------------------------------------------
# Parentheticals apps append to private/ad-free variants of a show title.
PAREN = re.compile(r"\s*\((private feed|premium feed|ad-?free|the binge)[^)]*\)", re.I)
# Trailing membership markers that distinguish a variant feed from its show.
SUFFIX = re.compile(
    r"\s*[:\-–—]?\s*(club|patrons[- ]only( episodes?( feed)?)?|bonus (content|episodes?)!?|"
    r"members?( only)?|archives?|ad-?free|premium|plus)\s*$", re.I)


def norm_title(t):
    """Family/episode title key: drop variant parentheticals, keep alnum only."""
    return re.sub(r"[^a-z0-9]+", "", PAREN.sub("", t or "").lower())


def strip_suffix(title):
    """norm_title after removing ONE trailing membership marker, or None if
    the title has no such marker. ("The Rest Is History Club" -> "therestishistory")"""
    t = PAREN.sub("", title or "")
    t2 = SUFFIX.sub("", t)
    return norm_title(t2) if t2 != t else None


# --- private feeds ----------------------------------------------------------
PRIVATE_URL = re.compile(
    r"/private/|[?&]auth=|[?&]access_token=|supportingcast\.fm/content/|"
    r"passport\.online|/members/|patreon\.com/rss/", re.I)


# --- dates ------------------------------------------------------------------
ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def iso_from_epoch(v):
    """Epoch (int/float) to ISO8601 UTC. NEVER assume the unit — detect it:
    values above 1e11 can only be milliseconds (1e11 s is the year 5138;
    1e11 ms is 1973, and no podcast data predates that)."""
    if not v:
        return None
    if v > 1e11:
        v /= 1000
    return datetime.fromtimestamp(v, tz=timezone.utc).strftime(ISO_FMT)


def iso_any(s):
    """Normalize a date STRING (ISO8601 or RFC2822) to ISO8601 UTC Z."""
    if not s:
        return None
    for parse in (datetime.fromisoformat, parsedate_to_datetime):
        try:
            d = parse(s.replace("Z", "+00:00") if parse is datetime.fromisoformat else s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc).strftime(ISO_FMT)
        except Exception:
            continue
    return None


def dur_to_sec(d):
    """itunes:duration to seconds: plain seconds, MM:SS, or HH:MM:SS."""
    if not d:
        return None
    try:
        parts = [float(x) for x in str(d).split(":")]
    except ValueError:
        return None
    s = 0
    for p in parts:
        s = s * 60 + p
    return s
