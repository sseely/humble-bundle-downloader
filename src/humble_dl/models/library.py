"""Library models for the legacy ``/api/v1/order/{gamekey}`` response.

The raw layer (``_Raw*``) mirrors the wire format and is validated with
unknown fields ignored: the API is external and undocumented, so additive
changes must not break runs (deliberate deviation from "reject unknown
fields"). The public domain models are flattened projections of it.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

EBOOK_PLATFORM = "ebook"
_PATH_SEPARATOR = "."


class SchemaDriftError(Exception):
    """Raised when an API payload no longer matches the expected schema.

    Attributes:
        path: Dotted path, in the raw input, of the first failing field.
    """

    def __init__(self, path: str, detail: str) -> None:
        super().__init__(f"Library schema drift at '{path}': {detail}")
        self.path = path


class FileVersion(BaseModel):
    """One downloadable file (a single format of an item)."""

    label: str
    url: str
    md5: str | None
    file_size: int | None


class Download(BaseModel):
    """A platform-specific group of files."""

    platform: str
    files: list[FileVersion]


class Subproduct(BaseModel):
    """One item (e.g. a book) within an order."""

    machine_name: str
    human_name: str
    downloads: list[Download]


class Order(BaseModel):
    """A purchase (bundle) and its items."""

    gamekey: str
    human_name: str
    subproducts: list[Subproduct]


class _RawModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _RawUrl(_RawModel):
    web: str | None = None


class _RawFile(_RawModel):
    name: str
    url: _RawUrl | None = None
    md5: str | None = None
    file_size: int | None = None


class _RawDownload(_RawModel):
    platform: str
    download_struct: list[_RawFile]


class _RawSubproduct(_RawModel):
    machine_name: str
    human_name: str
    downloads: list[_RawDownload]


class _RawProduct(_RawModel):
    human_name: str


class _RawOrder(_RawModel):
    gamekey: str
    product: _RawProduct
    subproducts: list[_RawSubproduct]


def _file_versions(raw_files: list[_RawFile]) -> list[FileVersion]:
    """Project raw files, skipping entries without a web URL."""
    return [
        FileVersion(label=f.name, url=f.url.web, md5=f.md5, file_size=f.file_size)
        for f in raw_files
        if f.url is not None and f.url.web
    ]


def _to_order(raw: _RawOrder) -> Order:
    return Order(
        gamekey=raw.gamekey,
        human_name=raw.product.human_name,
        subproducts=[
            Subproduct(
                machine_name=sub.machine_name,
                human_name=sub.human_name,
                downloads=[
                    Download(platform=d.platform, files=_file_versions(d.download_struct))
                    for d in sub.downloads
                ],
            )
            for sub in raw.subproducts
        ],
    )


def parse_order(raw: Mapping[str, Any]) -> Order:
    """Parse a raw legacy order payload into the domain model.

    Args:
        raw: Decoded JSON of ``/api/v1/order/{gamekey}``.

    Returns:
        The flattened ``Order``.

    Raises:
        SchemaDriftError: If required fields are missing or mistyped; ``path``
            is the dotted raw-input path of the first failing field.
    """
    try:
        return _to_order(_RawOrder.model_validate(raw))
    except ValidationError as exc:
        first = exc.errors()[0]
        path = _PATH_SEPARATOR.join(str(part) for part in first["loc"])
        raise SchemaDriftError(path, first["msg"]) from exc


def ebook_items(order: Order) -> list[tuple[Subproduct, list[FileVersion]]]:
    """List subproducts that have ebook files, with those files.

    Args:
        order: A parsed order.

    Returns:
        ``(subproduct, files)`` pairs in API order; subproducts with zero
        ebook files are omitted.
    """
    items: list[tuple[Subproduct, list[FileVersion]]] = []
    for sub in order.subproducts:
        files = [f for d in sub.downloads if d.platform == EBOOK_PLATFORM for f in d.files]
        if files:
            items.append((sub, files))
    return items
