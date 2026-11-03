"""Which photos are clearly free to copy, and so what the site may show of them.

A photo is "permitted" if its recorded licence clearly allows copying it and making something from
it: public domain, CC0, or a Creative Commons licence without No Derivatives (a texture is made from
the photos, so ND isn't enough). Non-commercial licences count, as the site is non-commercial. The
user's own uploads count. Anything else (Mountain Project, OpenBeta, web images, YouTube's standard
licence, or a licence not recorded) doesn't. license_considerations.md has why.

The admin page sets two things the main site follows, on the server as well as in the page:
  site.textures   all | permitted: a mesh's texture only if every photo in it is permitted, or else
                  one made again from only those (retexture.py), or none | none
  site.sources    all | link: photos and frames not permitted aren't served, only linked to |
                  permitted: those aren't shown at all
"""
from __future__ import annotations

import re

FREE = re.compile(r"\b(public domain|cc0|no known copyright restrictions|united states government work)\b", re.I)
CC = re.compile(r"\bcc[ -]?by\b|creative commons attribution|^by(-nc)?(-sa)?$", re.I)
NO_DERIVATIVES = re.compile(r"\bnd\b|no[ -]?deriv", re.I)


def permitted(entry: dict | None) -> bool:
    """Whether a photo's record (from photo_sources.json or sources.json) shows it's free to copy and adapt."""
    if not entry:
        return False
    if entry.get("source") == "upload":
        return True
    lic = (entry.get("license") or "").strip()
    if not lic:
        return False
    if FREE.search(lic):
        return True
    return bool(CC.search(lic)) and not NO_DERIVATIVES.search(lic)


def mesh_permitted(record: dict | None) -> bool | None:
    """Whether every photo of a mesh (provenance.load) is permitted: None if that isn't recorded."""
    if not record or not record.get("photos"):
        return None
    return all(permitted(p) for p in record["photos"])


def texture_mode(setting: str, upload: bool, record: dict | None, has_variant: bool) -> str:
    """How the site shows a mesh's texture: "full", "permitted" (the version textured only from permitted
    photos) or "hidden".

    Args:
        setting: site.textures.
        upload: the mesh was uploaded, not built here: it's the user's own.
        record: provenance.load() of the mesh.
        has_variant: retexture.py has made a version from permitted photos only.
    """
    if setting == "none":
        return "hidden"
    if setting == "all" or upload or mesh_permitted(record):
        return "full"
    return "permitted" if has_variant else "hidden"


def source_access(setting: str, entry: dict | None) -> str:
    """What the site may do with a photo or frame it holds a copy of: "show", "link" or "hide"."""
    if setting == "all" or permitted(entry):
        return "show"
    return "link" if setting == "link" else "hide"
