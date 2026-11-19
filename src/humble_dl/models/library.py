"""Library models and pure parsers for Humble Bundle flight (RSC) pages.

The library list page, a bundle detail page and the signed-URL server action
all arrive as Next.js flight text. Parsers here turn that text into domain
models. The raw layer (``_Flight*``) mirrors the wire format and is validated
with unknown fields ignored: the data is external and undocumented, so
additive changes must not break runs (deliberate deviation from "reject
unknown fields"). Legacy ``parse_order`` (the removed ``/api/v1/order``
endpoint) is retained only while planner tests still build orders from it.
"""

from collections.abc import Iterator, Mapping
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from humble_dl.api.rsc import KIND_JSON, parse_flight

EBOOK_PLATFORM = "ebook"
UNDEFINED = "$undefined"
KEY_BUNDLE_ITEMS = "bundleItems"
KEY_LIBRARY_ITEM = "userLibraryItem"
KEY_OK = "ok"
KEY_PRODUCT_ENTITLEMENT_ID = "productEntitlementId"
KEY_ID = "id"
KEY_TYPE = "type"
KEY_TITLE = "title"
TYPE_BUNDLE = "bundle"
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
    key: str | None = None


class Subproduct(BaseModel):
    """One item (e.g. a book) within an order."""

    machine_name: str
    human_name: str
    downloads: list[Download]
    entitlement_id: str | None = None


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


# --- flight-page parsing (pure) ----------------------------------------------


class BundleRef(BaseModel):
    """A bundle listed on the library page."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str


def _iter_dicts(value: object) -> Iterator[dict[str, object]]:
    """Yield every dict nested anywhere in ``value`` (depth-first, in order)."""
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        yield mapping
        for child in mapping.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in cast(list[object], value):
            yield from _iter_dicts(child)


def _json_dicts(flight: str) -> Iterator[dict[str, object]]:
    """Yield every dict found in any JSON row of ``flight``."""
    for row in parse_flight(flight):
        if row.kind == KIND_JSON:
            yield from _iter_dicts(row.payload)


def _drift_from(exc: ValidationError, prefix: str = "") -> SchemaDriftError:
    first = exc.errors()[0]
    path = _PATH_SEPARATOR.join(str(part) for part in first["loc"])
    return SchemaDriftError(f"{prefix}{path}", first["msg"])


def _bundle_ref(entry: object) -> BundleRef | None:
    if not isinstance(entry, dict):
        return None
    item = cast(dict[str, object], entry)
    if item.get(KEY_TYPE) != TYPE_BUNDLE:
        return None
    bundle_id = item.get(KEY_PRODUCT_ENTITLEMENT_ID) or item.get(KEY_ID)
    title = item.get(KEY_TITLE)
    if not isinstance(bundle_id, str) or not isinstance(title, str):
        return None
    return BundleRef(id=bundle_id, title=title)


def parse_library(flight: str) -> list[BundleRef]:
    """Extract the bundles listed on the library page.

    Args:
        flight: Flight text of the library list page.

    Returns:
        Bundles in page order, duplicates (by id) dropped.

    Raises:
        SchemaDriftError: If no ``bundleItems`` list exists in the payload.
    """
    found = False
    refs: dict[str, BundleRef] = {}
    for mapping in _json_dicts(flight):
        entries = mapping.get(KEY_BUNDLE_ITEMS)
        if not isinstance(entries, list):
            continue
        found = True
        for entry in cast(list[object], entries):
            ref = _bundle_ref(entry)
            if ref is not None:
                refs.setdefault(ref.id, ref)
    if not found:
        raise SchemaDriftError(KEY_BUNDLE_ITEMS, "no bundleItems list in library page")
    return list(refs.values())


class _FlightModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _undefined_to_none(cls, data: object) -> object:
        if isinstance(data, dict):
            return {
                key: (None if value == UNDEFINED else value)
                for key, value in cast(dict[str, object], data).items()
            }
        return data


class _FlightUrl(_FlightModel):
    web: str | None = None


class _FlightFile(_FlightModel):
    name: str
    url: _FlightUrl | None = None
    md5: str | None = None
    file_size: int | None = Field(default=None, alias="fileSize")


class _FlightDownload(_FlightModel):
    key: str
    platform: str
    download_struct: list[_FlightFile] = Field(alias="downloadStruct")


class _FlightBook(_FlightModel):
    id: str
    title: str
    downloads: list[_FlightDownload] | None = None


class _FlightEntitlements(_FlightModel):
    downloads: list[_FlightBook]


class _FlightItem(_FlightModel):
    title: str
    entitlements: _FlightEntitlements


class _FlightBundle(_FlightModel):
    item: _FlightItem = Field(alias=KEY_LIBRARY_ITEM)


def _flight_files(raw_files: list[_FlightFile]) -> list[FileVersion]:
    return [
        FileVersion(label=f.name, url=f.url.web, md5=f.md5, file_size=f.file_size)
        for f in raw_files
        if f.url is not None and f.url.web
    ]


def _machine_name(book: _FlightBook, downloads: list[_FlightDownload]) -> str:
    if not downloads:
        return book.id
    first = downloads[0]
    suffix = f"_{first.platform}"
    return first.key.removesuffix(suffix)


def _flight_subproduct(book: _FlightBook, downloads: list[_FlightDownload]) -> Subproduct:
    return Subproduct(
        machine_name=_machine_name(book, downloads),
        human_name=book.title,
        entitlement_id=book.id,
        downloads=[
            Download(platform=d.platform, key=d.key, files=_flight_files(d.download_struct))
            for d in downloads
        ],
    )


def parse_bundle(flight: str, bundle_id: str) -> Order:
    """Parse a bundle detail page into an ``Order``.

    File URLs are the bare filenames at this stage; callers swap in signed
    URLs with ``apply_signed_urls``. Books whose ``downloads`` is absent or
    ``"$undefined"`` are skipped.

    Args:
        flight: Flight text of the bundle detail page.
        bundle_id: Bundle identifier, used as the order's ``gamekey``.

    Returns:
        The flattened ``Order``.

    Raises:
        SchemaDriftError: If ``userLibraryItem`` is missing or mistyped;
            ``path`` is the dotted path of the first failing field.
    """
    for mapping in _json_dicts(flight):
        if KEY_LIBRARY_ITEM not in mapping:
            continue
        try:
            item = _FlightBundle.model_validate(mapping).item
        except ValidationError as exc:
            raise _drift_from(exc) from exc
        return Order(
            gamekey=bundle_id,
            human_name=item.title,
            subproducts=[
                _flight_subproduct(book, book.downloads)
                for book in item.entitlements.downloads
                if book.downloads is not None
            ],
        )
    raise SchemaDriftError(KEY_LIBRARY_ITEM, "no userLibraryItem in bundle page")


class _SignedData(_FlightModel):
    signed_urls: dict[str, dict[str, dict[str, str]]] = Field(alias="signedUrls")


class _SignedResponse(_FlightModel):
    data: _SignedData


def _signed_response_payload(text: str) -> dict[str, object] | None:
    for row in parse_flight(text):
        raw: object = row.payload
        if row.kind != KIND_JSON or not isinstance(raw, dict):
            continue
        payload = cast(dict[str, object], raw)
        if KEY_OK in payload:
            return payload
    return None


def parse_signed_urls(text: str) -> dict[str, dict[str, str]]:
    """Parse a signed-URL server action response.

    Args:
        text: Flight text of the response.

    Returns:
        ``{download_key: {filename: signed_url}}``, merged across machines.

    Raises:
        SchemaDriftError: If the ``ok`` row is missing or not ``true``, or
            ``data.signedUrls`` is malformed.
    """
    payload = _signed_response_payload(text)
    if payload is None or payload[KEY_OK] is not True:
        raise SchemaDriftError(KEY_OK, "signed-url response missing or not ok")
    try:
        response = _SignedResponse.model_validate(payload)
    except ValidationError as exc:
        raise _drift_from(exc) from exc
    merged: dict[str, dict[str, str]] = {}
    for by_key in response.data.signed_urls.values():
        for download_key, urls in by_key.items():
            merged.setdefault(download_key, {}).update(urls)
    return merged


def _signed_files(
    download: Download, signed: Mapping[str, Mapping[str, str]], missing: list[str]
) -> list[FileVersion]:
    urls: Mapping[str, str] = {}
    if download.key is not None:
        urls = signed.get(download.key, {})
    files: list[FileVersion] = []
    for file in download.files:
        if file.url in urls:
            files.append(file.model_copy(update={"url": urls[file.url]}))
        else:
            missing.append(file.url)
    return files


def apply_signed_urls(
    order: Order, signed: Mapping[str, Mapping[str, str]]
) -> tuple[Order, list[str]]:
    """Replace each file's bare filename with its signed URL.

    Args:
        order: Order whose file ``url`` values are bare filenames.
        signed: ``{download_key: {filename: signed_url}}``.

    Returns:
        A new order (input untouched) and the filenames with no signed URL,
        which are dropped from it.
    """
    missing: list[str] = []
    subproducts = [
        sub.model_copy(
            update={
                "downloads": [
                    d.model_copy(update={"files": _signed_files(d, signed, missing)})
                    for d in sub.downloads
                ]
            }
        )
        for sub in order.subproducts
    ]
    return order.model_copy(update={"subproducts": subproducts}), missing


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
