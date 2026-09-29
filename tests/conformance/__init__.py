"""Vendored IVOA schemas and the validator that uses them.

The XSDs under ``xsd/`` are the checkable artifact: a document that validates
against them is one ``stilts taplint`` and a registry harvester will accept.
They are fetched once and committed, so the test suite never reaches the
network.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

XSD_DIR = Path(__file__).parent / "xsd"

#: The schemas import each other by ivoa.net URL. Map each to the vendored copy
#: so validation stays offline.
_LOCAL_SCHEMAS = {
    "http://www.ivoa.net/xml/VOSICapabilities/v1.0": "VOSICapabilities-v1.0.xsd",
    "http://www.ivoa.net/xml/VOSIAvailability/v1.0": "VOSIAvailability-v1.0.xsd",
    "http://www.ivoa.net/xml/VOSITables/v1.0": "VOSITables-v1.0.xsd",
    "http://www.ivoa.net/xml/VODataService/v1.1": "VODataService-v1.1.xsd",
    "http://www.ivoa.net/xml/VOResource/v1.0": "VOResource-v1.0.xsd",
    "http://www.ivoa.net/xml/VOResource/VOResource-v1.0.xsd": "VOResource-v1.0.xsd",
    "http://www.ivoa.net/xml/TAPRegExt/v1.0": "TAPRegExt-v1.0.xsd",
    "http://www.ivoa.net/xml/UWS/v1.0": "UWS-v1.0.xsd",
    "http://www.ivoa.net/xml/STC/stc-v1.30.xsd": "stc-v1.30.xsd",
    "http://hea-www.harvard.edu/~arots/nvometa/v1.30/stc-v1.30.xsd": "stc-v1.30.xsd",
    "http://www.ivoa.net/xml/Xlink/xlink.xsd": "xlink.xsd",
}


class _OfflineResolver(etree.Resolver):
    """Serve every imported schema from ``xsd/``; fail loudly on anything else."""

    def resolve(self, system_url: str, public_id: str | None, context: object):  # type: ignore[no-untyped-def]
        filename = _LOCAL_SCHEMAS.get(system_url.rstrip("/"))
        if filename is None:
            return None
        return self.resolve_filename(str(XSD_DIR / filename), context)


def load_schema(filename: str) -> etree.XMLSchema:
    """Compile one vendored schema with its imports resolved locally."""
    parser = etree.XMLParser(load_dtd=False, no_network=True)
    parser.resolvers.add(_OfflineResolver())
    document = etree.parse(str(XSD_DIR / filename), parser)
    return etree.XMLSchema(document)


def assert_valid(xml: bytes | str, schema_filename: str) -> None:
    """Validate *xml*, raising with the schema's own message on failure."""
    schema = load_schema(schema_filename)
    payload = xml.encode("utf-8") if isinstance(xml, str) else xml
    document = etree.fromstring(payload, etree.XMLParser(no_network=True))
    if not schema.validate(document):
        errors = "\n".join(f"  line {e.line}: {e.message}" for e in schema.error_log)
        raise AssertionError(f"Document is not valid against {schema_filename}:\n{errors}")
